"""Reconciliation sweep — refresh tournaments with stuck incomplete data.

The pipeline's normal tournament refresh is event-driven: it only re-downloads
and re-parses a tournament's page (and bracket) when a *new match* for that
tournament arrives. This creates a gap: if a tournament's final match is parsed
while PlusForward still shows placeholder standings (empty 1st/2nd names), and
no further matches arrive, the stale page/bracket/standings are never
re-checked — even after PlusForward publishes the real winners later. The same
applies to brackets fetched mid-event (e.g. an EGB grand final still READY at
fetch time): the standings may already be complete, so nothing ever re-fetches
the bracket once the provider publishes the result.

This stage closes that gap with a schedule-driven, graduated cadence. Every
sweep it considers tournaments with INCOMPLETE final standings (some ranked
position has an empty player name) **or** an INCOMPLETE stored bracket
(complete=false) and force-refreshes those whose per-schedule cadence is due:

  - before schedule_end: refresh every minute        (RECONCILE_IN_SCHEDULE)
  - schedule_end .. +1 week: refresh every hour      (RECONCILE_POST_END)
  - past +1 week: stop scraping that tournament      (dropped)

A tournament with no parsed schedule (epoch dates) is treated as "unknown": it
is refreshed on the slow (post-end) cadence and dropped once its newest match
is more than a week old — so we don't scrape ancient never-completed events
forever. A tournament that becomes complete is removed from the working set.

Refresh goes through the normal TournamentResolver.resolve(force=True) path
(re-download + re-parse, updating standings) plus a forced bracket re-fetch.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta

from config import RECONCILE_INTERVAL, RECONCILE_POST_END_WEEKS
from src.db_client import Database
from src.fetcher import PageFetcher
from src.tournament_resolver import TournamentResolver
from src.bracket_fetcher import BracketFetcher, rebuild_missing as bracket_rebuild_missing

logger = logging.getLogger("pipeline.reconcile")

_EPOCH = datetime(1970, 1, 1)
# Sentinel for "schedule obviously missing": DB epoch values read back as
# 1970-01-01 00:00 or 03:00 depending on server tz; real events are post-2001.
_MIN_EVENT_DATE = datetime(1971, 1, 1)

# Cadence (seconds) per schedule phase.
IN_SCHEDULE_SECONDS = int(os.environ.get("RECONCILE_IN_SCHEDULE", "60"))     # 1 min
POST_END_SECONDS = int(os.environ.get("RECONCILE_POST_END", "3600"))          # 1 hr
# Tournaments without a usable schedule are probed on the slow cadence.
NO_SCHEDULE_SECONDS = int(os.environ.get("RECONCILE_NO_SCHEDULE", "3600"))

# Process-lifetime clock so the sweep only scans the DB every RECONCILE_INTERVAL.
_last_sweep = 0.0

# Per-tournament last-reconcile-attempt timestamp (process lifetime). Drives
# the graduated cadence: a tournament is refreshed when now - last_attempt >=
# its current cadence, or when it's newly discovered.
_last_attempt: dict[int, float] = {}


def _incomplete_rankings(rankings_json) -> bool:
    """True if rankings exist but at least one position has an empty player name."""
    if not rankings_json:
        return False
    if isinstance(rankings_json, str):
        try:
            entries = json.loads(rankings_json or "[]")
        except Exception:
            return False
    else:
        entries = rankings_json
    return bool(entries) and not all(e.get("player_name") for e in entries)


def _cadence_seconds(schedule_end, last_match, now: datetime) -> int | None:
    """Cadence (s) for a tournament, or None to stop scraping it.

    None means the tournament is past its scrape window and should be dropped.
    """
    # DB DateTime values are naive wall times in the server timezone
    # (Europe/Moscow since the 0ed9a99 migration), so epoch-derived "missing"
    # dates read back as 1970-01-01 03:00 — strictly greater than a literal
    # 1970-01-01 epoch. PlusForward events are all post-2001, so anything
    # before 1971 is a missing schedule, not a real date. (Before the
    # migration this comparison used to be against exact epoch and only
    # accidentally worked: 00:00 > 00:00 was False.)
    if schedule_end and schedule_end > _MIN_EVENT_DATE:
        if now < schedule_end:
            return IN_SCHEDULE_SECONDS
        if now < schedule_end + timedelta(weeks=RECONCILE_POST_END_WEEKS):
            return POST_END_SECONDS
        return None  # > a week past the scheduled end → stop
    # No usable schedule: probe on the slow cadence, drop once the newest match
    # is older than the post-end window (nothing new can be coming).
    if last_match and (now - last_match) > timedelta(weeks=RECONCILE_POST_END_WEEKS):
        return None
    return NO_SCHEDULE_SECONDS


def _incomplete_bracket_tournaments(db: Database) -> list[int]:
    """Tournament ids whose stored bracket is marked incomplete.

    A bracket fetched mid-event (e.g. an EGB grand final still READY at fetch
    time) stays incomplete forever unless something re-fetches it — the
    standings may already be complete, so the standings-driven sweep alone
    never revisits it. This is the bracket-side working set for that sweep.
    """
    rows = db.client.execute(
        "SELECT tournament_id, data FROM tournament_brackets FINAL"
    )
    out = []
    for tid, data in rows:
        try:
            d = json.loads(data or "{}")
        except Exception:
            continue
        if d.get("complete") is False:
            out.append(tid)
    return out


def _due_tournaments(db: Database, now: datetime) -> list[int]:
    """Incomplete-standings / incomplete-bracket tournaments due for refresh."""
    # Newest match time per tournament.
    last_match = {
        r[0]: r[1]
        for r in db.client.execute(
            "SELECT tournament_id, max(played_at) FROM matches FINAL "
            "WHERE tournament_id > 0 GROUP BY tournament_id"
        )
    }
    rows = db.client.execute(
        "SELECT tournament_id, schedule_end, rankings FROM tournaments FINAL "
        "WHERE rankings != '' AND rankings != '[]'"
    )
    due = []
    for tid, schedule_end, rankings in rows:
        if not _incomplete_rankings(rankings):
            continue  # complete — not our concern
        cadence = _cadence_seconds(schedule_end, last_match.get(tid), now)
        if cadence is None:
            # Past the scrape window — drop it from the working set so we stop
            # hitting PlusForward for a dead event.
            _last_attempt.pop(tid, None)
            continue
        last = _last_attempt.get(tid)
        if last is None or (time.time() - last) >= cadence:
            due.append(tid)
    # Incomplete brackets: refresh on the same graduated cadence so a bracket
    # fetched while its final was still pending gets re-checked after the
    # provider publishes the result. Dropped once past the scrape window.
    for tid in _incomplete_bracket_tournaments(db):
        det = db.get_tournament_details(tid)
        schedule_end = det.get("schedule_end") if det else None
        cadence = _cadence_seconds(schedule_end, last_match.get(tid), now)
        if cadence is None:
            _last_attempt.pop(tid, None)
            continue
        last = _last_attempt.get(tid)
        if last is None or (time.time() - last) >= cadence:
            due.append(tid)
    return list(dict.fromkeys(due))


def reconcile_once(force_all: bool = False) -> dict:
    """Run one reconciliation sweep. Returns stats dict.

    force_all: refresh every incomplete-standings / incomplete-bracket
    tournament regardless of the interval gate (used by manual runs / tests).
    """
    global _last_sweep
    now_ts = time.time()
    if not force_all and (now_ts - _last_sweep) < RECONCILE_INTERVAL:
        return {"due": False, "refreshed": 0, "scanned": 0}
    _last_sweep = now_ts

    # Cadence comparisons are all against DB DateTime wall times: naive values
    # in the ClickHouse server timezone (Europe/Moscow, same as this host)
    # since the 0ed9a99 migration. utcnow() skews every boundary by 3h.
    now = datetime.now()
    db = Database()
    try:
        due = _due_tournaments(db, now)
        logger.info(f"reconcile: {len(due)} tournament(s) due for refresh")
        refreshed = 0
        skipped = 0
        for tid in due:
            _last_attempt[tid] = now_ts
            try:
                fetcher = PageFetcher()
                resolver = TournamentResolver(db, fetcher)
                bracket_fetcher = BracketFetcher(db, fetcher)
                det = db.get_tournament_details(tid)
                standings_incomplete = bool(det) and _incomplete_rankings(det.get("rankings"))
                if standings_incomplete:
                    # Standings stuck: re-download + re-parse the page.
                    html = fetcher.fetch(f"https://www.plusforward.net/post/{tid}/")
                    if not html:
                        logger.warning(f"reconcile: no html for {tid}")
                        skipped += 1
                        continue
                    db.store_raw_post(tid, html, status="downloaded")
                    resolver.resolve(tid, force=True)
                # Bracket-only case (standings already complete): just
                # re-fetch the bracket — e.g. an EGB grand final that was
                # still READY when first fetched, published later.
                try:
                    bracket_fetcher.fetch_for_tournament_if_needed(tid, force=True)
                except Exception as e:
                    logger.warning(f"reconcile: bracket refresh failed for {tid}: {e}")
                det = db.get_tournament_details(tid)
                standings_ok = bool(det) and not _incomplete_rankings(det.get("rankings"))
                b = db.get_tournament_bracket(tid)
                bracket_ok = bool(b) and b.get("data", {}).get("complete") is True
                if standings_ok and bracket_ok:
                    logger.info(f"reconcile: {tid} standings + bracket now complete")
                    _last_attempt.pop(tid, None)
                refreshed += 1
            except Exception as e:
                logger.error(f"reconcile: refresh failed for {tid}: {e}")
                skipped += 1

        # Offline bracket backfill: rebuild parsed brackets from the
        # raw_brackets cache (e.g. right after a `reset.py parsed` wipe or a
        # bracket-normalization change). No provider requests; bounded per
        # sweep so a big first-time cache drains over a few cycles.
        try:
            n = bracket_rebuild_missing(db, limit=200)
            if n:
                logger.info(f"reconcile: rebuilt {n} bracket(s) from raw cache")
        except Exception as e:
            logger.warning(f"reconcile: bracket cache rebuild failed: {e}")

        return {"due": True, "scanned": len(due), "refreshed": refreshed,
                "skipped": skipped}
    finally:
        db.close()


def run_cycle() -> dict:
    """Stage entrypoint. Runs the sweep; returns stats for the runner loop.

    reconcile_once self-gates on RECONCILE_INTERVAL, so this returns quickly
    with {"due": False} most cycles. Returning empty=False keeps the runner on
    a short sleep so the interval is reached promptly; the cheap gate makes
    the repeated calls negligible.
    """
    res = reconcile_once()
    if not res.get("due"):
        return {"empty": True}
    if not res.get("refreshed"):
        return {"empty": True}
    return res

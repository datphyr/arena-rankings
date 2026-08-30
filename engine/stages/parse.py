"""Parse stage — event consumer.

Claims 'downloaded' rows from the queue and parses each via the proven
`match_parser._parse_post` kernel, then completes them as 'parsed' or skips
them (e.g. 'not a match', 'team format', 'parent index').

The parser's own `_parse_post` already handles the internal classification and
stores parsed results; our consumer adds claiming and backoff on retryable
failure.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.db_client import Database
from src.fetcher import PageFetcher
from src.match_parser import MatchDetailParser, _parse_post
from src.tournament_resolver import TournamentResolver
from engine.queue import PipelineQueue, ClaimedItem

logger = logging.getLogger("pipeline.parse")

# `_parse_post` returns (success, reason) where reason is a classifier:
#   ''            → success
#   'parent index' → permanent skip (aggregation page)
#   'team format' / 'invalid' / 'parse error' → permanent skip (bad match)
#   'vod pending'  → not yet processable; leave for later (retry, not skip)

PERMANENT_SKIP_REASONS = {"not a match", "team format", "invalid", "parent index", "parse error"}

# 'vod pending' = a VOD post whose match hasn't produced its match_vods rows
# yet. In steady operation VOD posts are discovered FROM parsed matches, so a
# pending VOD resolves on its next claim. After a `reset.py parsed` sweep the
# unattachable stragglers (VODs of team-format matches, e.g. the gLeagues 2v2
# posts of 2026-08-29/30) would fail forever — a logged retry every cycle at
# ~5-10 lines/s of log spam. So: log each pending post at most once a minute,
# and give up entirely after VOD_PENDING_GIVEUP_SECONDS (skipped with reason
# 'vod unattached'; a later `reset.py parsed` revives them alongside the
# late-stamped claim order that now puts VODs after their matches).
VOD_PENDING_GIVEUP_SECONDS = float(os.environ.get("VOD_PENDING_GIVEUP_SECONDS", "1800"))
_VOD_PENDING_FIRST: dict[int, float] = {}
_VOD_PENDING_LOGGED: dict[int, float] = {}


def _parse_one(post_id: int, raw_html: str) -> tuple[int, bool, str]:
    """Worker: parse one downloaded post. Returns (post_id, ok, reason)."""
    db = Database()
    try:
        parser = MatchDetailParser()
        fetcher = PageFetcher()
        resolver = TournamentResolver(db, fetcher)
        ok, reason = _parse_post(db, parser, resolver, None, post_id, raw_html)
        return (post_id, ok, reason)
    finally:
        db.close()


def _settle(q: PipelineQueue, item: ClaimedItem, ok: bool, reason: str, stats: dict) -> None:
    if ok:
        q.complete(item, "parsed")
        _VOD_PENDING_FIRST.pop(item.post_id, None)
        _VOD_PENDING_LOGGED.pop(item.post_id, None)
        stats["ok"] += 1
        return
    if reason == "vod pending":
        _settle_vod_pending(q, item, stats)
        return
    # Permanent classification → skip. Everything else transient → backoff.
    if reason in PERMANENT_SKIP_REASONS:
        q.skip(item, reason)
        stats["skipped"] += 1
    else:
        q.fail(item, reason or "parse error")
        stats["failed"] += 1


def _settle_vod_pending(q: PipelineQueue, item: ClaimedItem, stats: dict) -> None:
    """Handle a 'vod pending' failure: visible but rate-limited, bounded retries.

    No q.fail() call here — the row is already claimable and fail()'s per-cycle
    warning is the spam this handler exists to prevent. The rate-limited log
    below (once per minute per post) is the visible signal instead.
    """
    now = time.monotonic()
    first = _VOD_PENDING_FIRST.setdefault(item.post_id, now)
    if now - _VOD_PENDING_LOGGED.get(item.post_id, -1e9) >= 60:
        logger.warning(
            f"item {item.post_id} vod pending (no match_vods row yet); "
            f"pending for {now - first:.0f}s"
        )
        _VOD_PENDING_LOGGED[item.post_id] = now
    if now - first >= VOD_PENDING_GIVEUP_SECONDS:
        logger.warning(
            f"item {item.post_id} → skipped 'vod unattached' after "
            f"{now - first:.0f}s (its match never created match_vods rows)"
        )
        q.skip(item, "vod unattached")
        _VOD_PENDING_FIRST.pop(item.post_id, None)
        _VOD_PENDING_LOGGED.pop(item.post_id, None)
        stats["skipped"] += 1
        return
    # Leave the row claimable for the next cycle.
    stats["failed"] += 1


def run_cycle(workers: int = 1, limit: int = 0) -> dict:
    """Process one bounded batch of downloaded rows. Returns stats dict."""
    q = PipelineQueue(pending_status="downloaded")
    try:
        batch = q.claim(limit=limit or 20)
        if not batch:
            return {"claimed": 0, "ok": 0, "skipped": 0, "failed": 0, "empty": True}

        stats = {"claimed": len(batch), "ok": 0, "skipped": 0, "failed": 0}
        if workers <= 1:
            for item in batch:
                _pid, ok, reason = _parse_one(item.post_id, item.payload)
                _settle(q, item, ok, reason, stats)
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {
                    ex.submit(_parse_one, it.post_id, it.payload): it
                    for it in batch
                }
                for fut in as_completed(futures):
                    item = futures[fut]
                    _pid, ok, reason = fut.result()
                    _settle(q, item, ok, reason, stats)
        return stats
    finally:
        q.db.close()

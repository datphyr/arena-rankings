"""Rank stage — event-gated consumer.

Reuses the proven `rank.py`/`rankings_compute` kernels but only recomputes when
there is genuinely new parsed work:

- Gate cursor: the composite (played_at, match_id) point of the last rated
  match, read from rating_history (the DB is the cursor — no in-memory state).
  played_at orders the replay (the trustworthy chronology); match_id breaks
  ties (post ids are NOT chronological). Matches beyond the point = unrated
  work, detected instantly regardless of arrival order.
- A throttled per-game count sweep catches what no cursor can see: matches
  inserted *before* the rated point (backfilled old results), time edits,
  deletions. Tunable via RANK_CONSISTENCY_SECONDS (default 60).
- Only games that actually gained new matches (or are out-of-date, e.g. after
  a reset) are recomputed, so it doesn't burn CPU recomputing all 13 games
  every cycle.
- On a fresh/empty ratings state, `_check_match_state` still self-heals
  (recompute from scratch), preserving existing behaviour.
"""

from __future__ import annotations

import logging
import os
import time

from config import GLICKO2_PERIOD
from src.db_client import Database
from src.rankings_compute import compute_elo, compute_glicko2, store_ratings, _check_match_state

logger = logging.getLogger("pipeline.rank")

# Throttle for the consistency sweep in _any_new_work (out-of-order detection).
_consistency_check_ts = 0.0
_CONSISTENCY_CHECK_EVERY = int(os.environ.get("RANK_CONSISTENCY_SECONDS", "60"))

def _any_new_work(db: Database) -> bool:
    """True if there is parsed rating work.

    Primary cursor: the composite (played_at, match_id) point of the last
    rated match in Elo history — played_at orders the replay (the trustworthy
    chronology), match_id breaks ties. Anything beyond that point is unrated
    work, detected instantly and regardless of match_id order (late-posted
    results with high ids used to hide lower-id matches behind an id-only
    cursor).

    A cursor can't see matches inserted *before* the rated point (backfilled
    old results, time edits, deletions) — the throttled per-game count sweep
    (matches vs Elo history) catches those in ≤60s and triggers a backfill.
    """
    rated_t, rated_mid = db.get_last_processed_point("", "elo")
    if rated_t is None:
        return True  # nothing rated yet — everything is work
    if db.count_matches_after_point("", rated_t, rated_mid) > 0:
        return True

    global _consistency_check_ts
    now = time.time()
    if now - _consistency_check_ts < _CONSISTENCY_CHECK_EVERY:
        return False
    _consistency_check_ts = now
    games = [""]
    games.extend(r[0] for r in db.client.execute("SELECT name FROM games FINAL WHERE name != ''"))
    for game in games:
        state, _, _ = _check_match_state(db, game, "elo")
        if state != "up_to_date":
            return True
    return False


def run_cycle(game_filter: str = "", system: str = "both") -> dict:
    """Compute ratings only for games with new work. Returns stats dict."""
    db = Database()
    try:
        if game_filter:
            games = [game_filter]
        else:
            games = [""]
            game_rows = db.client.execute("SELECT name FROM games FINAL WHERE name != ''")
            games.extend([r[0] for r in game_rows])

        # No unrated matches beyond the last-rated point (and no throttled
        # count anomalies) → nothing to do. Report `empty` so the runner
        # sleeps the rank idle delay instead of burning CPU.
        if not _any_new_work(db):
            return {"empty": True, "up_to_date": len(games), "games": len(games)}

        # Pre-compute states BEFORE any computation (Elo/Glicko-2 consistency).
        states = {}
        for game in games:
            state, db_count, hist_count = _check_match_state(db, game, "elo")
            states[game] = (state, db_count, hist_count)

        total_ratings = 0
        up_to_date = 0
        changed = 0
        recomputed = 0

        for game in games:
            state, db_count, hist_count = states[game]

        for game in games:
            state, db_count, hist_count = states[game]

            # Up-to-date → nothing to recompute for this game (compute_elo
            # would consult the same state and return None, but this skips
            # its ratings load). A non-up-to-date state (reset/backfill/
            # out-of-order) is always recomputed.
            if state == "up_to_date":
                up_to_date += 1
                continue

            if system in ("elo", "both"):
                ratings = compute_elo(db, game, match_state=state, match_counts=(db_count, hist_count))
                if ratings:
                    store_ratings(db, ratings, game, "elo")
                    total_ratings += len(ratings) - 1
                    changed += 1
                    recomputed += 1

            if system in ("glicko2", "both"):
                ratings = compute_glicko2(db, game, period=GLICKO2_PERIOD, match_state=state, match_counts=(db_count, hist_count))
                if ratings:
                    store_ratings(db, ratings, game, "glicko2")
                    total_ratings += len(ratings) - 1

        if recomputed == 0:
            return {"empty": True, "up_to_date": up_to_date, "games": len(games)}
        return {
            "ratings": total_ratings,
            "up_to_date": up_to_date,
            "changed": changed,
            "recomputed": recomputed,
            "games": len(games),
        }
    finally:
        db.close()

"""Rank stage — event-gated consumer.

Reuses the proven `rank.py`/`rankings_compute` kernels but only recomputes when
there is genuinely new parsed work:

- Tracks a module-level watermark = the newest parsed match_id already reflected
  in ratings. When no match exists above the watermark, the stage reports
  `empty` → the runner sleeps the rank idle delay instead of recomputing.
- Only games that actually gained new matches (or are out-of-date, e.g. after
  a reset) are recomputed, so it doesn't burn CPU recomputing all 13 games
  every cycle.
- On a fresh/empty ratings state, `_check_match_state` still self-heals
  (recompute from scratch), preserving existing behaviour.
"""

from __future__ import annotations

import logging

from config import GLICKO2_PERIOD
from src.db_client import Database
from src.rankings_compute import compute_elo, compute_glicko2, store_ratings, _check_match_state

logger = logging.getLogger("pipeline.rank")

# Module-level watermark: newest match_id already folded into ratings. Updated
# after each successful cycle. Persists for the process lifetime (the stage
# process is long-lived); on restart it starts from 0, which just triggers one
# no-op up-to-date pass per game.
_watermark = 0


def _max_parsed_match(db: Database) -> int:
    """Newest match_id present in the parsed `matches` table (0 if none)."""
    rows = db.client.execute("SELECT max(match_id) FROM matches FINAL")
    return rows[0][0] if rows and rows[0][0] else 0


def _any_new_work(db: Database, watermark: int) -> bool:
    """True if there is a parsed match newer than the watermark."""
    rows = db.client.execute(
        "SELECT count() FROM matches FINAL WHERE match_id > %(mid)s",
        {"mid": watermark},
    )
    return bool(rows and rows[0][0] > 0)


def run_cycle(game_filter: str = "", system: str = "both") -> dict:
    """Compute ratings only for games with new work. Returns stats dict."""
    global _watermark
    db = Database()
    try:
        if game_filter:
            games = [game_filter]
        else:
            games = [""]
            game_rows = db.client.execute("SELECT name FROM games FINAL WHERE name != ''")
            games.extend([r[0] for r in game_rows])

        # No new parsed matches since the last pass → nothing to do. Report
        # `empty` so the runner sleeps the rank idle delay instead of burning
        # CPU recomputing every game.
        if _watermark and not _any_new_work(db, _watermark):
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

            # Skip games that are up-to-date AND have no matches newer than the
            # watermark — avoids the (expensive) full recompute when nothing
            # changed. A game that is not up-to-date (reset/backfill) is always
            # recomputed.
            if state == "up_to_date" and _watermark:
                max_id = db.get_max_match_id(game)
                if max_id <= _watermark:
                    up_to_date += 1
                    continue

            if system in ("elo", "both"):
                ratings = compute_elo(db, game, match_state=state, match_counts=(db_count, hist_count))
                if ratings:
                    store_ratings(db, ratings, game, "elo")
                    total_ratings += len(ratings) - 1
                    changed += 1
                    recomputed += 1
                elif state == "up_to_date":
                    up_to_date += 1

            if system in ("glicko2", "both"):
                ratings = compute_glicko2(db, game, period=GLICKO2_PERIOD, match_state=state, match_counts=(db_count, hist_count))
                if ratings:
                    store_ratings(db, ratings, game, "glicko2")
                    total_ratings += len(ratings) - 1

        # Advance the watermark to the newest parsed match (even if nothing was
        # recomputed, so the next cycle's `empty` check is correct).
        newest = _max_parsed_match(db)
        if newest > _watermark:
            _watermark = newest

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

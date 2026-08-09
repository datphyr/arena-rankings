#!/usr/bin/env python3
"""Wrapper for rankings computation — computes Elo and Glicko-2 ratings from parsed matches.

Usage:
    python rank.py                        # incremental run (auto-detects backfill)
    python rank.py --reset                # full recompute from scratch (nuke ratings + history)
    python rank.py --daemon               # daemon mode (loop forever)
    python rank.py --daemon --delay 30    # custom restart delay
    python rank.py --game "Quake Champions"  # specific game only
    python rank.py --system elo           # specific rating system
    python rank.py -v                     # verbose
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import DAEMON_RESTART_DELAY, GLICKO2_PERIOD
from src.daemon import run_daemon
from src.db_client import Database
from src.rankings_compute import compute_elo, compute_glicko2, store_ratings, _check_match_state


def cycle(args):
    db = Database()

    # Determine which games to compute ratings for
    if args.game:
        games = [args.game]
    else:
        games = [""]
        game_rows = db.client.execute(
            "SELECT DISTINCT game_name FROM matches FINAL WHERE game_name != ''"
        )
        games.extend([r[0] for r in game_rows])

    # Pre-compute match states for all games BEFORE any computation.
    # This ensures both Elo and Glicko-2 see the same state. If we checked
    # inside the loop, Elo would store its history first and Glicko-2 would
    # see the already-updated history and skip.
    if args.reset:
        states = {game: ("backfill", 0, 0) for game in games}
    else:
        states = {}
        for game in games:
            state, db_count, hist_count = _check_match_state(db, game, "elo")
            states[game] = (state, db_count, hist_count)

    total_ratings = 0

    for game in games:
        state, db_count, hist_count = states[game]

        if args.system in ("elo", "both"):
            ratings = compute_elo(db, game, full_recompute=args.reset, match_state=state, match_counts=(db_count, hist_count))
            if ratings:
                store_ratings(db, ratings, game, "elo")
                total_ratings += len(ratings) - 1  # subtract _history key

        if args.system in ("glicko2", "both"):
            ratings = compute_glicko2(db, game, full_recompute=args.reset, period=GLICKO2_PERIOD, match_state=state, match_counts=(db_count, hist_count))
            if ratings:
                store_ratings(db, ratings, game, "glicko2")
                total_ratings += len(ratings) - 1  # subtract _history key

    db.close()
    return f"{total_ratings} ratings"


def main():
    parser = argparse.ArgumentParser(description="Rankings computation wrapper")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run in daemon mode")
    parser.add_argument("--delay", type=int, default=DAEMON_RESTART_DELAY, help=f"Seconds between cycles (default: {DAEMON_RESTART_DELAY})")
    parser.add_argument("--game", "-g", type=str, default="", help="Game name filter (empty = all)")
    parser.add_argument("--system", "-s", type=str, default="both", choices=["elo", "glicko2", "both"], help="Rating system (default: both)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    parser.add_argument("--reset", action="store_true", help="Full recompute from scratch (clears all ratings and history)")
    args = parser.parse_args()

    sys.exit(run_daemon(
        name="rankings",
        cycle_fn=cycle,
        cycle_args=args,
        daemon=args.daemon,
        delay=args.delay,
        verbose=args.verbose,
    ))


if __name__ == "__main__":
    main()
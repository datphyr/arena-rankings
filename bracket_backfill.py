#!/usr/bin/env python3
"""One-time backfill of tournament brackets from external providers.

Scans all tournaments with cached PlusForward HTML, finds those with a bracket
source (Toornament or shambler link), and fetches + stores their bracket data
into the tournament_brackets table.

This is a one-time operation — new brackets are fetched automatically on the
normal parse pipeline (see src/match_parser.py). Run this only to populate
brackets for tournaments that were parsed before bracket support existed.

Usage:
    python bracket_backfill.py             # backfill all missing brackets
    python bracket_backfill.py --limit 20  # process at most 20 tournaments
    python bracket_backfill.py --refresh-days 30  # refetch brackets older than 30 days
    python bracket_backfill.py -v          # verbose
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.bracket_fetcher import backfill
from src.db_client import Database


def main():
    parser = argparse.ArgumentParser(description="Backfill tournament brackets from external providers")
    parser.add_argument("--limit", "-l", type=int, default=0, help="Max tournaments to process (0 = unlimited)")
    parser.add_argument("--refresh-days", type=int, default=None, help="Refetch brackets older than N days (default: only missing)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    import logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    for noisy in ("clickhouse_driver", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    db = Database()
    done = backfill(db, limit=args.limit, max_age_days=args.refresh_days)
    print(f"backfill complete: {done} brackets stored")


if __name__ == "__main__":
    main()

"""Backfill parsed tournament metadata (game, prize, formats, maplist, rankings).

Reads the already-stored raw_html from the tournaments table and re-parses it
into the parsed columns — NO network fetch. Idempotent: safe to re-run.

Run this AFTER backfill_tournament_html.py has populated raw_html.

Usage:
    python3 -m scripts.backfill_tournament_details
    # or:  python3 scripts/backfill_tournament_details.py
"""

import json
import logging
import sys
from pathlib import Path

# Allow running from repo root or from the scripts dir.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db_client import Database
from src.tournament_resolver import TournamentResolver

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill-details")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Only parse the N tournaments (test run)")
    parser.add_argument("--force", action="store_true",
                        help="Re-parse even if parsed columns are already populated")
    args = parser.parse_args()

    db = Database()
    resolver = TournamentResolver(db)

    # Tournaments that have HTML but no parsed metadata yet.
    if args.force:
        rows = db.client.execute(
            "SELECT tournament_id, name FROM tournaments FINAL WHERE raw_html != ''"
        )
    else:
        rows = db.client.execute(
            "SELECT tournament_id, name FROM tournaments FINAL "
            "WHERE raw_html != '' AND (game = '' OR rankings = '[]')"
        )
    if args.limit:
        rows = rows[: args.limit]
    total = len(rows)
    logger.info(f"{total} tournaments with HTML to parse")

    done = 0
    parsed = 0
    skipped = 0
    for i, (tid, name) in enumerate(rows, 1):
        html = db.get_tournament_html(tid)
        if not html:
            skipped += 1
            done += 1
            continue

        tier = resolver._parse_tier(html, tid)
        d = resolver._parse_tournament_details(html)
        db.upsert_tournament(
            tid, name, tier, raw_html=html,
            game=d["game"], prize_money=d["prize_money"],
            tourney_format=d["tourney_format"], match_format=d["match_format"],
            schedule_start=d["schedule_start"], schedule_end=d["schedule_end"],
            maplist=d["maplist"], rankings=d["rankings"],
        )
        parsed += 1
        done += 1

        if i % 200 == 0 or i == total:
            logger.info(f"progress {i}/{total} (parsed={parsed}, skipped={skipped})")

    logger.info(f"done: {done} processed, {parsed} parsed, {skipped} skipped (no html)")


if __name__ == "__main__":
    main()

"""Backfill raw_html for tournaments that have empty cached page HTML.

Root cause of the empty raw_html: store_parsed_match in match_parser.py called
upsert_tournament(..., without raw_html) AFTER the resolver resolved the tier had already
saved the HTML. Because tournaments is a ReplacingMergeTree keyed on tournament_id,
the second (empty-HTML) insert clobbered the one with HTML.

This script re-fetches the PlusForward page for every tournament with empty
raw_html, re-resolves its tier, and upserts with the raw_html preserved.

Usage:
    python3 -m scripts.backfill_tournament_html
    # or:  python3 scripts/backfill_tournament_html.py
"""

import logging
import sys
import time
from pathlib import Path

# Allow running from repo root or from the scripts dir.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import BASE_URL
from src.db_client import Database
from src.fetcher import PageFetcher
from src.tournament_resolver import TournamentResolver

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Only backfill the N newest tournaments (test run)")
    args = parser.parse_args()

    db = Database()
    fetcher = PageFetcher()
    resolver = TournamentResolver(db, fetcher)

    # All tournament ids that have empty raw_html, ordered newest first.
    rows = db.client.execute(
        "SELECT tournament_id, name FROM tournaments FINAL "
        "WHERE raw_html = '' ORDER BY tournament_id DESC"
    )
    if args.limit:
        rows = rows[: args.limit]
    total = len(rows)
    logger.info(f"{total} tournaments with empty raw_html to backfill")

    done = 0
    fetched = 0
    failed = 0
    for i, (tid, name) in enumerate(rows, 1):
        url = f"{BASE_URL}/post/{tid}/"
        # Force a network fetch (ignore any cached tier — we need the HTML).
        html = fetcher.fetch(url, max_retries=1)
        if not html:
            # Re-check DB — another run may have filled it.
            existing = db.get_tournament_html(tid)
            if existing:
                logger.debug(f"{tid} ({name}): filled by another run, skip")
            else:
                failed += 1
                logger.warning(f"{tid} ({name}): fetch failed")
            done += 1
            continue

        tier = resolver._parse_tier(html, tid)
        details = resolver._parse_tournament_details(html)
        db.upsert_tournament(
            tid, name, tier, raw_html=html,
            game=details["game"], prize_money=details["prize_money"],
            tourney_format=details["tourney_format"], match_format=details["match_format"],
            schedule_start=details["schedule_start"], schedule_end=details["schedule_end"],
            maplist=details["maplist"], rankings=details["rankings"],
        )
        fetched += 1
        done += 1

        if i % 50 == 0 or i == total:
            logger.info(f"progress {i}/{total} (fetched={fetched}, failed={failed})")

    logger.info(f"done: {done} processed, {fetched} fetched+stored, {failed} failed")
    # Show stats from the resolver.
    resolver.log_stats()


if __name__ == "__main__":
    main()

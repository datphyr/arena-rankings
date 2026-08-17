#!/usr/bin/env python3
"""One-off backfill: extract VOD links from already-parsed match HTML.

The VOD feature was added after the initial parse. This script re-reads the
match HTML already stored in raw_posts (status 'parsed') and extracts the
VODS section (vod_post_id, label, caster) into match_vods — no network calls,
no tournament re-resolution. The video embeds are filled in later by the
normal download + parse pipeline (which fetches each VOD post page).

Usage:
    python backfill_vods.py                 # backfill all parsed matches
    python backfill_vods.py --limit 1000    # limit matches processed
    python backfill_vods.py -v              # verbose
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.db_client import Database
from src.match_parser import MatchDetailParser


def backfill(limit: int = 0, verbose: bool = False) -> tuple[int, int]:
    """Extract VODs from parsed match HTML into match_vods.

    Returns (matches_with_vods, total_processed).
    """
    db = Database()
    parser = MatchDetailParser()

    # All parsed posts that are matches (have a match area). We re-parse just
    # the VODS section from their stored HTML.
    query = (
        "SELECT post_id, raw_html FROM raw_posts FINAL "
        "WHERE status = 'parsed' ORDER BY post_id ASC"
    )
    if limit > 0:
        query += f" LIMIT {limit}"
    rows = db.client.execute(query)

    total = len(rows)
    with_vods = 0
    vods_total = 0

    for i, (post_id, html) in enumerate(rows, 1):
        if not html or '<div class="match">' not in html:
            continue
        vods = parser._parse_vods(html)
        if vods:
            db.insert_match_vods(post_id, vods)
            with_vods += 1
            vods_total += len(vods)
        if verbose and (i % 1000 == 0 or i == total):
            print(f"{i}/{total} — {with_vods} matches with VODs ({vods_total} VODs)")

    db.close()
    return with_vods, total


def main():
    ap = argparse.ArgumentParser(description="Backfill VOD links from parsed match HTML")
    ap.add_argument("--limit", "-l", type=int, default=0, help="Max matches to process (0 = all)")
    ap.add_argument("--verbose", "-v", action="store_true", help="Progress output")
    args = ap.parse_args()

    with_vods, total = backfill(limit=args.limit, verbose=args.verbose)
    print(f"done: {with_vods} matches with VODs out of {total} processed")


if __name__ == "__main__":
    main()

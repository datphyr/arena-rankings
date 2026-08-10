#!/usr/bin/env python3
"""Backfill map results for already-parsed matches.

Re-parses the cached raw_html of matches that are already stored and
re-inserts their map rows (matches + match_maps only). This picks up maps
that were previously dropped — e.g. PlusForward's "unknown map" rows shown
as "?" (a 2:0 match used to store only 1 map because the "?" map didn't
match the old parser regex).

Why match_maps + matches only (no players/tournaments):
  - Player and tournament metadata are already correct and unchanged.
  - Skipping tournament upsert avoids clobbering the resolved tier, and
    avoids any network fetch to PlusForward (fully offline re-parse).

Idempotent: ReplacingMergeTree dedups on (played_at, match_id, map_index),
so re-inserting is safe to run repeatedly.

Usage:
    python3 -m scripts.backfill_maps               # all parsed matches
    python3 -m scripts.backfill_maps --limit 200   # test run
    python3 -m scripts.backfill_maps --match 94636 # single match
"""

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db_client import Database
from src.match_parser import MatchDetailParser

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill-maps")


def _worker(task: tuple):
    """Re-parse one match's raw_html and return (match_id, detail_or_None)."""
    match_id, played_at, raw_html = task
    parser = MatchDetailParser()
    detail = parser.parse(raw_html, match_id)
    return match_id, detail


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process N matches (test run)")
    parser.add_argument("--match", type=int, default=0,
                        help="Only process a single match_id")
    parser.add_argument("--workers", type=int, default=8,
                        help="Parser threads")
    args = parser.parse_args()

    db = Database()

    if args.match:
        rows = db.client.execute(
            "SELECT match_id, played_at, raw_html FROM match_registry FINAL "
            "WHERE match_id = %(m)s AND raw_html != ''",
            {"m": args.match},
        )
    else:
        rows = db.client.execute(
            "SELECT match_id, played_at, raw_html FROM match_registry FINAL "
            "WHERE raw_html != '' AND status = 'parsed' "
            "ORDER BY played_at DESC"
        )
    if args.limit:
        rows = rows[: args.limit]
    total = len(rows)
    db.close()

    if total == 0:
        logger.info("nothing to backfill")
        return
    logger.info(f"{total} matches to backfill | {args.workers}w")

    # Re-insert match + maps only (no players/tournaments, no network).
    out_db = Database()
    done = 0
    maps_fixed = 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_worker, t): t[0] for t in rows}
        for fut in as_completed(futures):
            mid = futures[fut]
            try:
                _, detail = fut.result()
            except Exception as e:
                logger.warning(f"reparse error {mid}: {e}")
                done += 1
                continue

            if detail is not None:
                out_db.insert_match(detail)
                if detail.maps:
                    out_db.insert_match_maps(detail.match_id, detail.maps, detail.played_at)
                    maps_fixed += 1

            done += 1
            if done % 1000 == 0 or done == total:
                elapsed = time.time() - start
                rate = done / elapsed if elapsed > 0 else 0
                logger.info(f"{done}/{total} — {maps_fixed} matches with maps, {rate:.0f}/s")

    out_db.close()
    elapsed = time.time() - start
    logger.info(f"done: {done}/{total} in {elapsed:.0f}s — {maps_fixed} matches have map rows")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""One-off recovery: restore tournament metadata clobbered by a bad schedule fix.

The midnight-crossing schedule fix inserted rows with empty name/tier/game/rankings
(via upsert_tournament with only schedule columns), which — because ClickHouse
ReplacingMergeTree replaces whole rows — wiped those fields for the affected
tournaments. This re-parses each affected tournament's FULL metadata from its
cached raw_html (raw_posts), preserving the corrected schedule.

Safe to run repeatedly (idempotent): re-parses from cached HTML, never hits the
network.
"""
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db_client import Database
from src.tournament_resolver import TournamentResolver
from src.fetcher import PageFetcher

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("restore_tournaments")


def main():
    db = Database()
    try:
        empty = db.client.execute("SELECT tournament_id FROM tournaments FINAL WHERE name = ''")
        ids = [r[0] for r in empty]
        print(f"{len(ids)} tournament(s) with empty name to restore")
        fetcher = PageFetcher()
        resolver = TournamentResolver(db, fetcher)
        restored = 0
        for tid in ids:
            html = db.raw_post_get_html(tid)
            if not html:
                print(f"  !! {tid}: no cached html, skipping")
                continue
            try:
                # _resolve_from_html re-parses name/tier/game/rankings/schedule
                # from the cached page and upserts the full row.
                resolver._resolve_from_html(html, tid)
                restored += 1
            except Exception as e:
                print(f"  !! {tid}: failed: {e}")
        print(f"restored {restored}/{len(ids)}")

        # Verify no empty-name tournaments remain.
        still_empty = db.client.execute("SELECT count() FROM tournaments FINAL WHERE name = ''")
        print("tournaments still with empty name:", still_empty[0][0])
    finally:
        db.close()


if __name__ == "__main__":
    main()

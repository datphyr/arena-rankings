"""Match download — download a single match page and store raw HTML in ClickHouse.

Usage:
    python -m src.match_download <match_id> [match_id ...]
    python -m src.match_download 94499
"""

import logging
import sys
from datetime import datetime
from typing import Optional

from config import BASE_URL
from src.db_client import Database
from src.fetcher import PageFetcher

logger = logging.getLogger(__name__)


class MatchDownloader:
    """Download individual match pages from PlusForward."""

    def __init__(self, fetcher: PageFetcher = None):
        self._fetcher = fetcher or PageFetcher()

    def download_and_store(self, db: Database, match_id: int) -> bool:
        """Download a match page and store its raw HTML in the registry.

        Inserts a new row with downloaded HTML (ReplacingMergeTree dedupes by played_at, match_id).

        Args:
            db: Database client.
            match_id: PlusForward post ID.

        Returns:
            True if successfully downloaded and stored, False otherwise.
        """
        url = f"{BASE_URL}/post/{match_id}/"
        html = self._fetcher.fetch(url)
        if html is None:
            return False

        # Get existing played_at to preserve it
        rows = db.client.execute(
            "SELECT played_at FROM match_registry FINAL WHERE match_id = %(mid)s LIMIT 1",
            {"mid": match_id},
        )
        played_at = rows[0][0] if rows else datetime(1970, 1, 1)

        # Insert new row with downloaded HTML (ReplacingMergeTree keeps latest by (played_at, match_id))
        db.client.execute(
            "INSERT INTO match_registry "
            "(match_id, played_at, raw_html, status) VALUES",
            [(match_id, played_at, html, "downloaded")],
        )

        return True


def download_matches(match_ids: list[int]) -> tuple[int, int]:
    """Download multiple matches by ID.

    Args:
        match_ids: List of match IDs to download.

    Returns:
        (success_count, failure_count)
    """
    fetcher = PageFetcher()
    downloader = MatchDownloader(fetcher)
    db = Database()
    success = 0
    failure = 0

    try:
        for mid in match_ids:
            if downloader.download_and_store(db, mid):
                success += 1
            else:
                failure += 1
    finally:
        db.close()

    logger.info(f"Download complete: {success} success, {failure} failure")
    return success, failure


if __name__ == "__main__":
    import argparse
    from config import LOG_LEVEL

    parser = argparse.ArgumentParser(description="Download single match(es) from PlusForward")
    parser.add_argument("match_ids", nargs="+", type=int, help="Match ID(s) to download")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for noisy in ("clickhouse_driver", "urllib3", "asyncio", "tzlocal"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    ok, fail = download_matches(args.match_ids)
    sys.exit(0 if fail == 0 else 1)
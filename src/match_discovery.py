"""Match discovery — scrape PlusForward matchlist pages to find all match IDs.

Scrapes https://www.plusforward.net/matchlist/results/?page=N pages,
extracts match IDs from the `listmatch` entries, and stores
them in the ClickHouse `match_registry` table.

Resume strategy:
  1. Always start from page 1 and scan forward (newest first).
  2. On each page, extract match IDs.
  3. If all IDs on a page are already in the registry → we've caught up,
     stop the forward scan.
  4. Continue from last_known_page+1 backward to discover older matches.
  5. last_known_page always tracks the furthest page we've ever scanned.

Usage:
    python -m src.match_discovery              # full scan
    python -m src.match_discovery --max-pages 50  # limit pages
    python -m src.match_discovery --forward-only  # only scan from page 1
"""

import logging
import re
import sys
from datetime import datetime
from typing import Optional

from config import MATCHLIST_URL
from src.db_client import Database
from src.fetcher import PageFetcher

logger = logging.getLogger(__name__)

# Regex to extract match IDs + timestamps from listmatch anchors
# Entry: <a href="/post/<id>/..." class="listmatch " ... title="02 Aug 2026 17:45 UTC">...
LISTMATCH_RE = re.compile(
    r'href="/post/(\d+)/[^"]*?"[^>]*class="listmatch\s*"[^>]*?>.*?title="([^"]*\d{4} \d{2}:\d{2} UTC)"',
    re.DOTALL,
)
# Parse timestamp like "02 Aug 2026 17:45 UTC" -> datetime
TS_FMT = "%d %b %Y %H:%M UTC"


def parse_match_timestamp(ts_str: str) -> datetime:
    """Parse PlusForward matchlist timestamp string to datetime."""
    try:
        return datetime.strptime(ts_str, TS_FMT)
    except ValueError:
        return datetime(2000, 1, 1)

# Pagination: detect "next page" link
NEXT_PAGE_RE = re.compile(r'href="[^"]*page=(\d+)[^"]*"[^>]*>next page</a>')


class MatchlistFetcher:
    """Fetch PlusForward matchlist pages and parse match IDs."""

    def __init__(self, fetcher: PageFetcher = None):
        self._fetcher = fetcher or PageFetcher()

    def fetch_page(self, page_num: int) -> Optional[str]:
        """Fetch a matchlist results page. Returns HTML text or None (404/empty)."""
        url = f"{MATCHLIST_URL}?page={page_num}&cat=0&status=&search=&evsearch="
        return self._fetcher.fetch(url)

    @staticmethod
    def parse_matches(html: str) -> list[tuple[int, datetime]]:
        """Extract (match_id, played_at) pairs from matchlist HTML."""
        results = []
        for mid_str, ts_str in LISTMATCH_RE.findall(html):
            results.append((int(mid_str), parse_match_timestamp(ts_str)))
        return results

    @staticmethod
    def has_next_page(html: str) -> bool:
        """Check if the page has a 'next page' link."""
        return bool(NEXT_PAGE_RE.search(html))


def discover_matches(
    max_pages: int = 0,
    forward_only: bool = False,
) -> int:
    """Discover matches from PlusForward matchlist.

    Resume strategy:
      1. Always start from page 1 and scan forward until all matches on a page
         are already known (catches new matches + serves as forward scan).
      2. After forward scan, continue from last_known_page+1 backward to find
         older matches we haven't seen yet.
      3. last_known_page always tracks the furthest page we've ever scanned.

    Args:
        max_pages: Maximum number of pages to fetch (0 = unlimited).
        forward_only: Only scan from page 1 (skip backward scan).

    Returns:
        Total number of new matches registered.
    """
    fetcher = MatchlistFetcher()
    db = Database()
    total_new = 0
    pages_fetched = 0

    try:
        existing_ids = db.registry_get_all_ids()
        known_count = len(existing_ids)
        last_known_page = db.get_last_known_page()
        initial_count = known_count
        logger.debug(f"registry: {known_count} known, resume from page {last_known_page}")

        # Phase 1: Forward scan — always start from page 1
        page = 1
        forward_caught_up = False

        while True:
            if max_pages > 0 and pages_fetched >= max_pages:
                pass  # max_pages reached
                break

            html = fetcher.fetch_page(page)
            if html is None:
                logger.error(f"forward: page {page} fetch failed")
                break

            pages_fetched += 1
            matches = fetcher.parse_matches(html)

            if not matches:
                break

            new_pairs = [(mid, ts) for mid, ts in matches if mid not in existing_ids]
            new_matches = [p[0] for p in new_pairs]
            new_timestamps = [p[1] for p in new_pairs]
            known_on_page = len(matches) - len(new_matches)

            if new_matches:
                added = db.register_matches(new_matches, played_at_timestamps=new_timestamps)
                total_new += added
                existing_ids.update(new_matches)
                logger.debug(
                    f"forward: page {page} — {added} new ({total_new} total)"
                )
            else:
                pass  # all known

            # Save progress — only update if this page is further than what we know
            if page > last_known_page:
                db.set_last_known_page(page)
                last_known_page = page

            # If all matches on this page are known, we've caught up
            if known_on_page == len(matches):
                forward_caught_up = True
                break

            if not fetcher.has_next_page(html):
                forward_caught_up = True
                break

            page += 1

        if forward_only:
            logger.debug(f"forward: {total_new} new, {pages_fetched} pages")
            return total_new

        # Phase 2: Backward scan — continue from where we left off to find older matches
        # Skip if we've already reached the bottom (no more older pages exist)
        backward_complete = db.get_discovery_state("backward_complete", "0") == "1"
        if backward_complete:
            pass  # backward already complete
        else:
            backward_start = max(last_known_page + 1, page + 1)
            if not forward_caught_up:
                backward_start = page + 1
            page = backward_start
            consecutive_known = 0

            while True:
                if max_pages > 0 and pages_fetched >= max_pages:
                    logger.debug(f"backward: max_pages limit ({max_pages})")
                    break

                html = fetcher.fetch_page(page)
                if html is None:
                    logger.error(f"backward: page {page} fetch failed")
                    break

                pages_fetched += 1
                matches = fetcher.parse_matches(html)

                if not matches:
                    db.set_discovery_state("backward_complete", "1")
                    break

                new_pairs = [(mid, ts) for mid, ts in matches if mid not in existing_ids]
                new_matches = [p[0] for p in new_pairs]
                new_timestamps = [p[1] for p in new_pairs]
                if new_matches:
                    added = db.register_matches(new_matches, played_at_timestamps=new_timestamps)
                    total_new += added
                    existing_ids.update(new_matches)
                    consecutive_known = 0
                    logger.debug(
                        f"backward: page {page} — {added} new ({total_new} total)"
                    )
                else:
                    consecutive_known += 1

                # Save progress — only update if this page is further than what we know
                if new_matches and page > last_known_page:
                    db.set_last_known_page(page)
                    last_known_page = page
                if consecutive_known >= 3:
                    logger.info(f"backward: bottom at page {page}")
                    db.set_discovery_state("backward_complete", "1")
                    break

                if not fetcher.has_next_page(html):
                    db.set_discovery_state("backward_complete", "1")
                    break

                page += 1

        logger.info(f"done: {total_new} new, {pages_fetched} pages")
        return total_new

    finally:
        final = db.registry_count_total()
        db.close()
        logger.debug(f"registry: {final} total (was {initial_count})")



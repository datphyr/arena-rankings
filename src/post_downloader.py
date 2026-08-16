"""Post downloader — download ALL PlusForward posts sequentially into raw_posts.

Scans /post/1, /post/2, ... and stores each page's HTML in the raw_posts table
(status 'downloaded'). There is no separate discovery stage: we download
straight away, oldest→newest, and stop when we hit the post wall (a post whose
title is the generic "Post | Plus Forward" placeholder, i.e. an invalid/nonexistent
post id). The post before the first invalid one is the last real post.

Resume strategy:
  1. Start from last_scanned_post + 1 (0 on a fresh database).
  2. Fetch each post; store its HTML in raw_posts.
  3. On the first invalid post (generic title), check if it's a deleted block
     or the real end: count consecutive invalid posts. If WALL_CONSECUTIVE
     consecutive invalid posts, it's the wall — latch download_complete and stop.
     Otherwise skip past the deleted block to the first valid post.
  4. On the next run we start from the same last_scanned_post and stop
     immediately if it's still invalid (i.e. no new posts since last run).

Sidebar cookies are sent on every request (see PageFetcher), so the server omits
the sidebar HTML and each page is roughly half the size.

Usage:
    python -m src.post_downloader                  # download all posts
    python -m src.post_downloader --limit 1000     # limit posts per run
    python -m src.post_downloader -v               # verbose
"""

import logging
import re
import time
from datetime import datetime
from typing import Optional

from src.db_client import Database
from src.fetcher import PageFetcher
from config import WALL_CONSECUTIVE

logger = logging.getLogger(__name__)

# A valid post has a descriptive <title>; an invalid/nonexistent post id returns
# the generic placeholder title. This is the reliable "wall" signal (the
# "Invalid post id." body text is unreliable due to transient partial loads).
GENERIC_TITLE = "Post | Plus Forward"
TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.IGNORECASE)


def _is_wall(fetcher: "PostDownloader", post_id: int, consec_bad: int) -> tuple[bool, bool]:
    """Check if we've hit the real wall.

    Returns (True, scan_done) if post_id starts the real end of the sequence,
    or (False, False) when it's a deleted block and we need to keep scanning.
    When scan_done is True, the caller should stop (post_id *is* the wall).
    """
    # Simple sequential check: count consecutive invalid posts from post_id.
    bad = 0
    i = post_id
    while i < post_id + WALL_CONSECUTIVE:
        html = fetcher.fetch_post(i)
        if html is not None and is_valid_post(html):
            # Found a valid post within the window — not the wall, it's a
            # deleted block. Caller should skip to this valid post.
            return False, False
        bad += 1
        i += 1

    # Counted WALL_CONSECUTIVE consecutive invalid posts without finding any
    # valid one. It's the real wall.
    if bad == WALL_CONSECUTIVE:
        return True, True
    return False, False

# Scheduled time of a match, from the match page Date block:
#   <div class="title">Date</div><div class="date"><div>13:15 UTC</div><div>16th August 2026</div></div>
DATE_BLOCK_RE = re.compile(
    r'<div class="title">Date</div><div class="date"><div>([^<]+)</div><div>([^<]+)</div></div>',
    re.DOTALL,
)


def is_valid_post(html: str) -> bool:
    """True if the fetched page is a real post (descriptive title)."""
    m = TITLE_RE.search(html or "")
    return bool(m) and m.group(1).strip() != GENERIC_TITLE


def parse_scheduled_time(html: str) -> Optional[datetime]:
    """Extract the scheduled match time from a post's HTML, or None.

    Parses the Date block (e.g. '13:15 UTC' + '16th August 2026') into a naive
    UTC datetime. Used to decide when to re-fetch an upcoming ('not played')
    match — we only retry after its scheduled time has passed.
    """
    m = DATE_BLOCK_RE.search(html or "")
    if not m:
        return None
    time_str = m.group(1).replace(" UTC", "").strip()
    date_str = re.sub(r"(\d+)(?:st|nd|rd|th)", r"\1", m.group(2)).strip()
    try:
        return datetime.strptime(f"{date_str} {time_str}", "%d %B %Y %H:%M")
    except ValueError:
        return None


class PostDownloader:
    """Download PlusForward posts into raw_posts."""

    def __init__(self, fetcher: PageFetcher = None):
        self._fetcher = fetcher or PageFetcher()

    def fetch_post(self, post_id: int) -> Optional[str]:
        """Fetch a single post page. Returns HTML or None on failure."""
        url = f"https://www.plusforward.net/post/{post_id}/"
        return self._fetcher.fetch(url)


def _fetch_worker(post_id: int) -> tuple[int, Optional[str]]:
    """Fetch one post in a worker thread (each thread its own fetcher)."""
    f = PageFetcher()
    return post_id, f.fetch(f"https://www.plusforward.net/post/{post_id}/")


def download_posts(limit: int = 0, workers: int = 1) -> tuple[int, int, bool]:
    """Download posts into raw_posts, oldest→newest, until the post wall.

    Fetches posts concurrently (each worker its own rate-limited fetcher) but
    stores them in order so the wall is detected correctly. Stops at a block of
    WALL_CONSECUTIVE consecutive invalid posts and latches download_complete.

    Args:
        limit: Maximum posts to download this run (0 = unlimited).
        workers: Number of concurrent download workers (1 = sequential).

    Returns:
        (downloaded_count, wall_hit, complete) where wall_hit is True if we hit
        the invalid-post wall this run, and complete is True if the full history
        has been scanned (wall hit at least once, latched).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    db = Database()
    fetcher = PostDownloader()
    try:
        start = db.get_last_scanned_post() + 1
        # If we've already hit the wall, skip scanning entirely.
        if db.is_download_complete():
            logger.info("download already complete, nothing to do")
            return 0, True, True

        post_id = start
        downloaded = 0
        wall_hit = False
        consec_bad = 0  # consecutive invalid posts since last valid
        start_time = time.time()
        workers = max(1, workers)

        # Fetch in batches of `workers` concurrent posts, process in order.
        while True:
            if limit > 0 and downloaded >= limit:
                break

            # Build the next batch of post ids to fetch concurrently, skipping
            # posts already stored (e.g. a tournament the resolver fetched
            # early) so we don't re-download them.
            batch_ids = []
            pid = post_id
            while len(batch_ids) < workers:
                if limit > 0 and downloaded + len(batch_ids) >= limit:
                    break
                if not db.raw_post_exists(pid):
                    batch_ids.append(pid)
                else:
                    db.set_last_scanned_post(pid)
                pid += 1
            if not batch_ids:
                # All posts in this range are already stored — advance past them
                # and keep scanning (there may be more posts beyond).
                post_id = pid
                if limit > 0 and downloaded >= limit:
                    break
                continue

            # Fetch the batch concurrently, collect results in id order.
            results = {}
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(_fetch_worker, i): i for i in batch_ids}
                for fut in as_completed(futures):
                    pid, html = fut.result()
                    results[pid] = html

            # Process in order; stop at the first invalid post.
            for i in batch_ids:
                html = results.get(i)
                if html is None:
                    logger.error(f"post {i} fetch failed, stopping")
                    return downloaded, wall_hit, db.is_download_complete()
                if not is_valid_post(html):
                    consec_bad += 1
                    db.set_last_scanned_post(i)
                    if consec_bad >= WALL_CONSECUTIVE:
                        logger.info(f"wall at post {i} (invalid after {consec_bad} bad posts), last valid = {i - 1}")
                        db.set_discovery_state("download_complete", "1")
                        wall_hit = True
                        return downloaded, wall_hit, True
                    # Not yet WALL_CONSECUTIVE — might be a deleted block.
                    # Advance to next post in batch; counter carries forward.
                else:
                    # Valid post — reset counter and store it.
                    consec_bad = 0
                    db.store_raw_post(i, html, "downloaded")
                    db.set_last_scanned_post(i)
                    downloaded += 1
                    if downloaded % 100 == 0:
                        elapsed = time.time() - start_time
                        rate = downloaded / elapsed if elapsed > 0 else 0
                        logger.debug(f"{downloaded} posts ({i}) — {rate:.1f}/s")
                # Advance post_id tracking: batch ended at the highest id in this batch.
            post_id = batch_ids[-1] + 1

        elapsed = time.time() - start_time
        logger.info(f"done: {downloaded} downloaded, wall={wall_hit}, {elapsed:.0f}s")

        # Re-fetch upcoming ('not played') matches whose scheduled time has
        # passed, so the parse stage can pick them up once they finish.
        refreshed = refresh_upcoming(db, fetcher)
        if refreshed:
            logger.info(f"refreshed {refreshed} upcoming matches")

        return downloaded, wall_hit, db.is_download_complete()
    finally:
        db.close()


def refresh_upcoming(db: Database, fetcher: "PostDownloader" = None, now: datetime = None) -> int:
    """Re-fetch 'not played' matches whose scheduled time has passed.

    Upcoming matches are stored with status 'skipped' and reason 'not played'.
    Once their scheduled time passes, we re-download the page; if it now has
    scores (the match finished), we mark it 'downloaded' so the parse stage
    processes it. If it still has no scores, we leave it as 'not played' and
    retry on a later cycle.

    Returns the number of posts re-fetched.
    """
    if fetcher is None:
        fetcher = PostDownloader()
    now = now or datetime.utcnow()

    rows = db.client.execute(
        "SELECT post_id, raw_html FROM raw_posts FINAL "
        "WHERE status = 'skipped' AND reason = 'not played'"
    )
    refreshed = 0
    for post_id, html in rows:
        scheduled = parse_scheduled_time(html)
        if scheduled is None or scheduled > now:
            continue  # not due yet — retry later
        new_html = fetcher.fetch_post(post_id)
        if new_html is None or not is_valid_post(new_html):
            continue
        # Store the fresh page; if it now has scores the parse stage will
        # process it, otherwise it stays 'not played' (re-fetched next cycle).
        db.store_raw_post(post_id, new_html, "downloaded")
        refreshed += 1
    return refreshed

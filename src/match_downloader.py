"""Match downloader (discovery mode) — batch download discovered matches.

Reads match IDs registered by matchlist discovery (raw_posts status
'discovered'), fetches each match page, and stores the HTML in raw_posts with
status 'downloaded' (ready for the parser). sort_time is preserved from
discovery (the matchlist timestamp) — the parser overwrites it with the
page-derived value once parsed.

This is the discovery download mode: we only fetch the match pages that
discovery found, not every post id sequentially.

Usage:
    python -m src.match_downloader                    # download all pending
    python -m src.match_downloader --workers 3        # use 3 workers
    python -m src.match_downloader --limit 100         # limit to 100 matches
    python -m src.match_downloader --workers 5 --limit 1000
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from src.db_client import Database
from src.fetcher import PageFetcher

logger = logging.getLogger(__name__)


def get_pending_matches(db: Database, limit: int = 0) -> list[tuple[int, datetime]]:
    """Get matches that haven't been downloaded yet (status 'discovered').

    Returns list of (match_id, sort_time) tuples, chronological first.
    """
    return db.raw_post_get_discovered(limit)


def download_batch(
    workers: int = 1,
    limit: int = 0,
) -> tuple[int, int]:
    """Download pending discovered matches in batch.

    Args:
        workers: Number of concurrent download workers.
        limit: Maximum matches to download (0 = unlimited).

    Returns:
        (success_count, failure_count)
    """
    db = Database()
    pending = get_pending_matches(db, limit)
    db.close()

    total = len(pending)
    if total == 0:
        logger.debug("no pending matches")
        return 0, 0

    first_ts = pending[0][1] if pending else None
    last_ts = pending[-1][1] if pending else None
    logger.info(f"{total} pending | {workers}w")

    success = 0
    failure = 0
    start_time = time.time()

    # Log progress at ~10% intervals or every 2000 items
    progress_interval = max(total // 10, 2000) if total > 1000 else 100

    if workers <= 1:
        # Single-threaded: simple interruptible loop
        fetcher = PageFetcher()
        db = Database()
        try:
            for i, item in enumerate(pending, 1):
                mid, ts = item
                try:
                    if _download_one(db, fetcher, mid, ts):
                        success += 1
                    else:
                        failure += 1
                        logger.warning(f"download failed: {mid}")
                except Exception as e:
                    logger.error(f"download failed: {mid}: {e}")
                    failure += 1

                if i % progress_interval == 0 or i == total:
                    elapsed = time.time() - start_time
                    rate = i / elapsed if elapsed > 0 else 0
                    pct = i * 100 // total
                    logger.debug(f"{i}/{total} ({pct}%) — {success} ok, {failure} fail, {rate:.1f}/s")
        finally:
            db.close()
    else:
        # Multi-threaded: ThreadPoolExecutor with interruptible completion
        def worker_task(match_id: int, sort_time: datetime) -> bool:
            worker_db = Database()
            worker_fetcher = PageFetcher()
            try:
                return _download_one(worker_db, worker_fetcher, match_id, sort_time)
            finally:
                worker_db.close()

        executor = ThreadPoolExecutor(max_workers=workers)
        futures = {executor.submit(worker_task, mid, ts): (mid, ts) for mid, ts in pending}

        try:
            for i, future in enumerate(as_completed(futures), 1):
                mid, ts = futures[future]
                try:
                    if future.result():
                        success += 1
                    else:
                        failure += 1
                        logger.warning(f"download failed: {mid}")
                except Exception as e:
                    logger.error(f"download failed: {mid}: {e}")
                    failure += 1

                if i % progress_interval == 0 or i == total:
                    elapsed = time.time() - start_time
                    rate = i / elapsed if elapsed > 0 else 0
                    pct = i * 100 // total
                    logger.debug(f"{i}/{total} ({pct}%) — {success} ok, {failure} fail, {rate:.1f}/s")
        except KeyboardInterrupt:
            logger.warning(f"interrupted, cancelling {len(futures)} downloads")
            for f in futures:
                f.cancel()
            raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    elapsed = time.time() - start_time
    logger.info(f"done: {success} ok, {failure} fail, {total} total, {elapsed:.0f}s")
    return success, failure


def _download_one(db: Database, fetcher: PageFetcher, match_id: int, sort_time: datetime) -> bool:
    """Fetch a single match page and store it in raw_posts as 'downloaded'.

    Preserves the discovery-time sort_time (matchlist timestamp) so the parser
    can order chronologically even before it parses the page.
    """
    url = f"https://www.plusforward.net/post/{match_id}/"
    html = fetcher.fetch(url)
    if html is None:
        return False
    db.store_raw_post(match_id, html, "downloaded", sort_time=sort_time)
    return True

"""Match downloader — batch download matches using a worker pool.

Iterates over all matches in the registry that haven't been downloaded yet
(status = 'discovered'), and downloads them using
concurrent workers.

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
from src.match_download import MatchDownloader

logger = logging.getLogger(__name__)


def get_pending_matches(db: Database, limit: int = 0) -> list[tuple[int, datetime]]:
    """Get matches that haven't been downloaded yet.

    Returns list of (match_id, played_at) tuples.
    """
    query = (
        "SELECT match_id, played_at FROM match_registry FINAL "
        "WHERE status = 'discovered' "
        "ORDER BY played_at DESC"
    )
    if limit > 0:
        query += f" LIMIT {limit}"

    rows = db.client.execute(query)
    return [(row[0], row[1]) for row in rows]


def download_batch(
    workers: int = 1,
    limit: int = 0,
) -> tuple[int, int]:
    """Download pending matches in batch.

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
        downloader = MatchDownloader()
        db = Database()
        try:
            for i, item in enumerate(pending, 1):
                mid, ts = item
                try:
                    if downloader.download_and_store(db, mid):
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
        def worker_task(match_id: int) -> bool:
            worker_db = Database()
            worker_downloader = MatchDownloader()
            try:
                return worker_downloader.download_and_store(worker_db, match_id)
            finally:
                worker_db.close()

        executor = ThreadPoolExecutor(max_workers=workers)
        futures = {executor.submit(worker_task, mid): (mid, ts) for mid, ts in pending}

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

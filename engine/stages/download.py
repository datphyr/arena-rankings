"""Download stage — event consumer.

Claims 'discovered' rows from the queue, fetches each match page via
`match_downloader._download_one` (the same proven kernel `download_batch` uses),
then completes them as 'downloaded'.

Unlike the old polling wrapper, this stage only works when the queue has
pending items (event-driven), claims bounded batches with lease crash-safety,
and routes failures to backoff/dead-letter.

Concurrency model: workers each use their own Database + PageFetcher for the
fetch (mirroring `download_batch`); the status-flip (complete/skip/fail) runs
on the coordinator thread, which is the single writer for this stage.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.db_client import Database
from src.fetcher import PageFetcher
from src import match_downloader as md
from engine.queue import PipelineQueue, ClaimedItem

logger = logging.getLogger("pipeline.download")


def _fetch_one(post_id: int, sort_time) -> tuple[int, bool, str]:
    """Worker: fetch one post with its own DB/fetcher. Returns (post_id, ok, reason)."""
    db = Database()
    fetcher = PageFetcher()
    try:
        ok = md._download_one(db, fetcher, post_id, sort_time)
        return (post_id, ok, "" if ok else "fetch failed")
    except Exception as e:
        return (post_id, False, str(e)[:200])
    finally:
        db.close()


def _settle(q: PipelineQueue, item: ClaimedItem, ok: bool, reason: str, stats: dict) -> None:
    if ok:
        q.complete(item, "downloaded")
        stats["ok"] += 1
    else:
        # Fetch failure is retryable (network blip) → backoff/dead-letter.
        q.fail(item, reason)
        stats["failed"] += 1


def run_cycle(workers: int = 1, limit: int = 0, max_attempts: int = 5) -> dict:
    """Process one bounded batch of discovered rows. Returns stats dict."""
    q = PipelineQueue(pending_status="discovered", max_attempts=max_attempts)
    try:
        batch = q.claim(limit=limit or 20)
        if not batch:
            return {"claimed": 0, "ok": 0, "failed": 0, "empty": True}

        stats = {"claimed": len(batch), "ok": 0, "failed": 0}
        if workers <= 1:
            for item in batch:
                _pid, ok, reason = _fetch_one(item.post_id, item.sort_time)
                _settle(q, item, ok, reason, stats)
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {
                    ex.submit(_fetch_one, it.post_id, it.sort_time): it
                    for it in batch
                }
                for fut in as_completed(futures):
                    item = futures[fut]
                    _pid, ok, reason = fut.result()
                    _settle(q, item, ok, reason, stats)
        return stats
    finally:
        q.db.close()

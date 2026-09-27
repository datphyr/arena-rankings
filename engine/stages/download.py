"""Download stage — event consumer.

Claims 'discovered' rows from the queue, fetches each match page via
`match_downloader._download_one` (the same proven kernel `download_batch` uses),
then completes them as 'downloaded'.

Unlike the old polling wrapper, this stage only works when the queue has
pending items (event-driven), claims bounded batches, and routes failures to
backoff.

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
from src.post_downloader import refresh_upcoming
from engine.queue import PipelineQueue, ClaimedItem

logger = logging.getLogger("pipeline.download")

# Max 'not played' posts to re-fetch per cycle. Bounded so a large backlog of
# long-past matches can't monopolise a cycle; the per-post cooldown in
# refresh_upcoming keeps the sweep from re-hitting the same pages every cycle.
UPCOMING_REFETCH_LIMIT = 20


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
        # Fetch failure is retryable (network blip) → backoff.
        q.fail(item, reason)
        stats["failed"] += 1


def run_cycle(workers: int = 1, limit: int = 0) -> dict:
    """Process one bounded batch of discovered rows. Returns stats dict.

    After the discovered batch, also re-fetches any 'not played' matches whose
    scheduled time has passed. A match discovered before it was played only
    becomes parseable once its page gains scores, and nothing else in the
    pipeline ever revisits a 'skipped' row — without this sweep those matches
    sat at 'not played' forever even after the result was published on the
    site.
    """
    q = PipelineQueue(pending_status="discovered")
    try:
        batch = q.claim(limit=limit or 20)
        stats = {"claimed": len(batch), "ok": 0, "failed": 0, "refreshed": 0}
        if not batch:
            stats["empty"] = True
        elif workers <= 1:
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

        # Re-fetch finished-but-stuck upcoming matches (own DB/fetcher).
        try:
            udb = Database()
            try:
                stats["refreshed"] = refresh_upcoming(
                    udb, limit=UPCOMING_REFETCH_LIMIT)
            finally:
                udb.close()
        except Exception as e:
            logger.warning(f"upcoming refresh failed: {e}")

        if stats["refreshed"]:
            stats.pop("empty", None)
        return stats
    finally:
        q.db.close()

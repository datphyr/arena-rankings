"""Parse stage — event consumer.

Claims 'downloaded' rows from the queue and parses each via the proven
`match_parser._parse_post` kernel, then completes them as 'parsed' or skips
them (e.g. 'not a match', 'team format', 'parent index').

The parser's own `_parse_post` already handles the internal classification and
stores parsed results; our consumer adds lease-based claiming, backoff on
retryable failure, and dead-lettering for permanent errors.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.db_client import Database
from src.fetcher import PageFetcher
from src.match_parser import MatchDetailParser, _parse_post
from src.tournament_resolver import TournamentResolver
from engine.queue import PipelineQueue, ClaimedItem

logger = logging.getLogger("pipeline.parse")

# `_parse_post` returns (success, reason) where reason is a classifier:
#   ''            → success
#   'parent index' → permanent skip (aggregation page)
#   'team format' / 'invalid' / 'parse error' → permanent skip (bad match)
#   'vod pending'  → not yet processable; leave for later (retry, not skip)

PERMANENT_SKIP_REASONS = {"not a match", "team format", "invalid", "parent index", "parse error"}


def _parse_one(post_id: int, raw_html: str) -> tuple[int, bool, str]:
    """Worker: parse one downloaded post. Returns (post_id, ok, reason)."""
    db = Database()
    try:
        parser = MatchDetailParser()
        fetcher = PageFetcher()
        resolver = TournamentResolver(db, fetcher)
        ok, reason = _parse_post(db, parser, resolver, None, post_id, raw_html)
        return (post_id, ok, reason)
    finally:
        db.close()


def _settle(q: PipelineQueue, item: ClaimedItem, ok: bool, reason: str, stats: dict) -> None:
    if ok:
        q.complete(item, "parsed")
        stats["ok"] += 1
        return
    # Permanent classification → skip. 'vod pending' → retry later (it needs
    # the match parsed first). Everything else transient → backoff/dead-letter.
    if reason in PERMANENT_SKIP_REASONS:
        q.skip(item, reason)
        stats["skipped"] += 1
    else:
        q.fail(item, reason or "parse error")
        stats["failed"] += 1


def run_cycle(workers: int = 1, limit: int = 0, max_attempts: int = 5) -> dict:
    """Process one bounded batch of downloaded rows. Returns stats dict."""
    q = PipelineQueue(pending_status="downloaded", max_attempts=max_attempts)
    try:
        batch = q.claim(limit=limit or 20)
        if not batch:
            return {"claimed": 0, "ok": 0, "skipped": 0, "failed": 0, "empty": True}

        stats = {"claimed": len(batch), "ok": 0, "skipped": 0, "failed": 0}
        if workers <= 1:
            for item in batch:
                _pid, ok, reason = _parse_one(item.post_id, item.payload)
                _settle(q, item, ok, reason, stats)
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {
                    ex.submit(_parse_one, it.post_id, it.payload): it
                    for it in batch
                }
                for fut in as_completed(futures):
                    item = futures[fut]
                    _pid, ok, reason = fut.result()
                    _settle(q, item, ok, reason, stats)
        return stats
    finally:
        q.db.close()

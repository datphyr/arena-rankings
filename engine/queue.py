"""Pipeline work queue — claiming over the raw_posts status machine.

The queue treats `raw_posts.status` as an explicit state machine
(discovered → downloaded → parsed | skipped). Stages see only
`claim / complete / fail / skip / pending_count`.

On failure a row is left in the queue and retried with exponential backoff.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from src.db_client import Database
from src.backoff import Backoff

logger = logging.getLogger("pipeline")


@dataclass
class ClaimedItem:
    """A single work item handed to a stage."""

    post_id: int
    payload: Optional[str]          # raw_html for download/parse stages
    status: str                     # the pre-work status ('discovered', 'downloaded')
    sort_time: Optional[datetime] = None


class PipelineQueue:
    """Claim/complete/fail queue over raw_posts.

    Not thread-safe by itself; each worker thread/coroutine should use its own
    instance (it opens its own Database), or callers must serialize access.
    """

    def __init__(
        self,
        pending_status: str,
        backoff: Optional[Backoff] = None,
    ):
        self.pending_status = pending_status   # e.g. 'discovered' or 'downloaded'
        self.backoff = backoff or Backoff()
        self.db = Database()

    # ------------------------------------------------------------------
    # Public API used by stages
    # ------------------------------------------------------------------
    def pending_count(self) -> int:
        """Number of rows ready to process right now (quick count)."""
        rows = self.db.client.execute(
            "SELECT count() FROM raw_posts FINAL "
            f"WHERE status = '{self.pending_status}'"
        )
        return rows[0][0] if rows else 0

    def claim(self, limit: int = 10) -> list[ClaimedItem]:
        """Claim up to `limit` pending rows.

        Returns a list of ClaimedItem. If no rows are available, returns [].
        """
        query = (
            "SELECT post_id, raw_html, status, sort_time FROM raw_posts FINAL "
            f"WHERE status = '{self.pending_status}' "
            "ORDER BY sort_time ASC, post_id ASC LIMIT %(limit)s"
        )
        rows = self.db.client.execute(query, {"limit": limit})
        return [
            ClaimedItem(post_id=r[0], payload=r[1], status=r[2], sort_time=r[3])
            for r in rows
        ]

    @staticmethod
    def _pid(item) -> int:
        """Accept either a ClaimedItem or a raw post_id."""
        return item.post_id if isinstance(item, ClaimedItem) else item

    def complete(self, item, next_status: str, reason: str = "") -> None:
        """Flip a claimed post to its next status (e.g. 'downloaded' → 'parsed')."""
        pid = self._pid(item)
        self.db.raw_post_mark(pid, next_status, reason)
        self.backoff.reset()

    def skip(self, item, reason: str) -> None:
        """Permanently skip a post that is not processable (not an error)."""
        pid = self._pid(item)
        self.db.raw_post_mark(pid, "skipped", reason)

    def fail(self, item: ClaimedItem, error: str) -> None:
        """Record a failure. Leave the row in the queue; backoff gates the retry."""
        wait = self.backoff.next_delay()
        logger.warning(
            f"item {item.post_id} failed: {error}; retry in {wait:.0f}s"
        )

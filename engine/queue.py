"""Pipeline work queue — crash-safe claiming over the raw_posts status machine.

The queue treats `raw_posts.status` as an explicit state machine
(discovered → downloaded → parsed | skipped) and adds three things on top:

1. **Lease claiming (`locked_until`)** — a worker atomically claims rows by
   stamping a `locked_until` timestamp. Rows claimed by a dead worker are
   re-claimed once their lease expires. This gives crash-safe "at-most-once
   in flight, at-least-once overall" semantics without row-level locks
   (ClickHouse ReplacingMergeTree has none).

2. **Backoff on failure** — a transient failure leaves the row locked with a
   short lease; a permanent failure moves it to the `failed_posts` dead-letter
   table after N attempts.

3. **Dead-letter** — `failed_posts` quarantines rows that exhausted their
   retries, so they stop blocking the queue and stay visible for manual retry.

The `locked_until` column is isolated to THIS module: stages only ever see
`claim / complete / fail / pending_count`. If `locked_until` needs to be
removed later, only the SQL inside here changes — no stage code touches it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from src.db_client import Database
from src.backoff import Backoff, CircuitBreaker

logger = logging.getLogger("pipeline")

DEFAULT_LEASE_SECONDS = 30      # how long a claim stays locked
DEFAULT_MAX_ATTEMPTS = 5        # before moving a row to the dead-letter

# The lease column may not exist yet (added by migration). We probe once and
# remember, so callers don't pay a schema check on every claim.
_lease_checked = False
_lease_enabled = True


def _ensure_lease_column(db: Database) -> bool:
    """Ensure raw_posts has the `locked_until` lease column. Returns enabled.

    This is the single place that touches the schema for the lease. If the
    column can't be added (e.g. permissions), we fall back to lease-less
    claiming (still correct for single-worker stages, just not crash-safe
    against concurrent workers).
    """
    global _lease_checked, _lease_enabled
    if _lease_checked:
        return _lease_enabled
    try:
        db.client.execute(
            "ALTER TABLE raw_posts ADD COLUMN IF NOT EXISTS "
            "locked_until Nullable(DateTime) AFTER reason"
        )
        _lease_enabled = True
    except Exception as e:  # pragma: no cover - depends on CH version/permissions
        logger.warning(f"could not add locked_until lease column: {e}; using lease-less queue")
        _lease_enabled = False
    _lease_checked = True
    return _lease_enabled


@dataclass
class ClaimedItem:
    """A single work item handed to a stage."""

    post_id: int
    payload: Optional[str]          # raw_html for download/parse stages
    status: str                     # the pre-work status ('discovered', 'downloaded')
    sort_time: Optional[datetime] = None
    attempts: int = field(default=0, init=False)


class PipelineQueue:
    """Claim/complete/fail queue over raw_posts + failed_posts.

    Not thread-safe by itself; each worker thread/coroutine should use its own
    instance (it opens its own Database), or callers must serialize access.
    """

    def __init__(
        self,
        pending_status: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff: Optional[Backoff] = None,
    ):
        self.pending_status = pending_status   # e.g. 'discovered' or 'downloaded'
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.backoff = backoff or Backoff()
        self.db = Database()

    # ------------------------------------------------------------------
    # Lease helpers (isolated here so locked_until is removable)
    # ------------------------------------------------------------------
    def _lease_clause(self) -> str:
        if _ensure_lease_column(self.db):
            return "AND (locked_until IS NULL OR locked_until < now())"
        return ""

    def _claim_lock_sql(self, post_ids: list[int]) -> None:
        if not _ensure_lease_column(self.db) or not post_ids:
            return
        self.db.client.execute(
            "ALTER TABLE raw_posts UPDATE locked_until = now() + INTERVAL %(sec)s SECOND "
            "WHERE post_id IN %(ids)s",
            {"sec": self.lease_seconds, "ids": post_ids},
        )

    # ------------------------------------------------------------------
    # Public API used by stages
    # ------------------------------------------------------------------
    def pending_count(self) -> int:
        """Number of rows ready to process right now (quick count)."""
        rows = self.db.client.execute(
            "SELECT count() FROM raw_posts FINAL "
            f"WHERE status = '{self.pending_status}' {self._lease_clause()}"
        )
        return rows[0][0] if rows else 0

    def claim(self, limit: int = 10) -> list[ClaimedItem]:
        """Claim up to `limit` pending rows, stamping their lease.

        Returns a list of ClaimedItem. A claimed row is invisible to other
        workers until its lease expires. If no rows are available, returns [].
        """
        query = (
            "SELECT post_id, raw_html, status, sort_time FROM raw_posts FINAL "
            f"WHERE status = '{self.pending_status}' {self._lease_clause()} "
            "ORDER BY sort_time ASC, post_id ASC LIMIT %(limit)s"
        )
        rows = self.db.client.execute(query, {"limit": limit})
        items = [
            ClaimedItem(post_id=r[0], payload=r[1], status=r[2], sort_time=r[3])
            for r in rows
        ]
        if items:
            self._claim_lock_sql([it.post_id for it in items])
        return items

    @staticmethod
    def _pid(item) -> int:
        """Accept either a ClaimedItem or a raw post_id."""
        return item.post_id if isinstance(item, ClaimedItem) else item

    def complete(self, item, next_status: str, reason: str = "") -> None:
        """Flip a claimed post to its next status (e.g. 'downloaded' → 'parsed')."""
        pid = self._pid(item)
        self.db.raw_post_mark(pid, next_status, reason)
        # Clear the lease so the row is no longer claim-blocked (harmless if
        # the lease column is absent).
        if _ensure_lease_column(self.db):
            self.db.client.execute(
                "ALTER TABLE raw_posts UPDATE locked_until = NULL WHERE post_id = %(pid)s",
                {"pid": pid},
            )
        self.backoff.reset()

    def skip(self, item, reason: str) -> None:
        """Permanently skip a post that is not processable (not an error)."""
        pid = self._pid(item)
        self.db.raw_post_mark(pid, "skipped", reason)
        self._clear_lease(pid)

    def fail(self, item: ClaimedItem, error: str) -> None:
        """Record a failure. Transient → re-queue (lease expires & retried).
        Permanent (attempts exhausted) → dead-letter.

        Returns nothing; stages call `retry_now`/`is_dead` if they need to know.
        """
        item.attempts += 1
        if item.attempts >= self.max_attempts:
            self._dead_letter(item, error)
        else:
            # Transient: leave the row in the queue; set a lease far enough
            # out that backoff naturally gates the retry. Clearing it would
            # let the next claim pick it up immediately.
            wait = self.backoff.next_delay()
            if _ensure_lease_column(self.db):
                self.db.client.execute(
                    "ALTER TABLE raw_posts UPDATE locked_until = now() + INTERVAL %(sec)s SECOND "
                    "WHERE post_id = %(pid)s",
                    {"sec": max(wait, self.lease_seconds), "pid": item.post_id},
                )
            logger.warning(
                f"item {item.post_id} failed (attempt {item.attempts}): {error}; "
                f"retry in {wait:.0f}s"
            )

    def _clear_lease(self, post_id: int) -> None:
        if _ensure_lease_column(self.db):
            self.db.client.execute(
                "ALTER TABLE raw_posts UPDATE locked_until = NULL WHERE post_id = %(pid)s",
                {"pid": post_id},
            )

    def _dead_letter(self, item: ClaimedItem, error: str) -> None:
        """Move a permanently-failing row to failed_posts (dead-letter)."""
        self.db.client.execute(
            "INSERT INTO failed_posts (post_id, stage, status, error, attempts, "
            "first_seen, last_error, raw_html) VALUES",
            [(
                item.post_id,
                self.pending_status,
                item.status,
                error[:500],
                item.attempts,
                datetime.utcnow(),
                datetime.utcnow(),
                item.payload or "",
            )],
        )
        # Remove from the active queue so it stops blocking.
        self.db.client.execute(
            "ALTER TABLE raw_posts DELETE WHERE post_id = %(pid)s",
            {"pid": item.post_id},
        )
        logger.error(f"item {item.post_id} dead-lettered after {item.attempts} attempts: {error}")

    def retry_dead_letter(self, post_id: int) -> bool:
        """Manually re-queue a dead-lettered post as 'discovered' for reprocessing."""
        rows = self.db.client.execute(
            "SELECT post_id, raw_html, error FROM failed_posts FINAL WHERE post_id = %(pid)s",
            {"pid": post_id},
        )
        if not rows:
            return False
        _pid, raw_html, _err = rows[0]
        self.db.store_raw_post(post_id, raw_html, "discovered")
        self.db.client.execute(
            "ALTER TABLE failed_posts DELETE WHERE post_id = %(pid)s",
            {"pid": post_id},
        )
        return True

    def dead_letter_count(self) -> int:
        rows = self.db.client.execute("SELECT count() FROM failed_posts FINAL")
        return rows[0][0] if rows else 0

    def list_dead_letters(self, limit: int = 50) -> list[dict]:
        rows = self.db.client.execute(
            "SELECT post_id, stage, status, error, attempts, first_seen, last_error "
            "FROM failed_posts FINAL ORDER BY last_error DESC LIMIT %(limit)s",
            {"limit": limit},
        )
        return [
            {
                "post_id": r[0],
                "stage": r[1],
                "status": r[2],
                "error": r[3],
                "attempts": r[4],
                "first_seen": r[5],
                "last_error": r[6],
            }
            for r in rows
        ]

"""Shared stage runner — the per-stage event loop.

Replaces the old `src/daemon.py run_daemon` polling loop. Each stage process
runs one of these. Behaviour:

- **Event-gated:** the stage calls its `has_work()` predicate; if no work, it
  sleeps a short `idle_delay` (default 5s) instead of a blind 60s poll. Work is
  claimed in bounded batches, so a busy stage processes continuously while an
  idle one sleeps cheaply.
- **Backoff on error:** failures grow the wait via exponential backoff.
- **Real crash semantics:** a *recoverable* error is logged + backed off; a
  `fatal` flag from the cycle causes a non-zero exit → the supervisor restarts
  the process (systemd Restart=on-failure now means something).
- **Status reporting:** after each cycle writes a snapshot to pipeline_status.
- **Graceful shutdown:** SIGTERM/SIGINT finish the current cycle then exit 0.
"""

from __future__ import annotations

import logging
import signal
import time
from typing import Callable, Optional

from engine import status as st

logger = logging.getLogger("pipeline.runner")

_running = True


def _handle_signal(signum, frame):
    global _running
    _running = False


def _install_signals() -> None:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)


def _interruptible_sleep(seconds: float) -> None:
    """Sleep in 0.2s slices so SIGTERM during sleep exits promptly."""
    end = time.time() + seconds
    while _running and time.time() < end:
        time.sleep(min(0.2, end - time.time()))


class _StageBackoff:
    """Exponential backoff for a stage, reset on success."""

    def __init__(self, max_delay: float):
        self._max = max_delay
        self._attempts = 0

    def reset(self) -> None:
        self._attempts = 0

    def next_delay(self) -> float:
        self._attempts += 1
        return min(self._max, 2.0 ** self._attempts)


def _report(name: str, stats: dict) -> None:
    """Write a status snapshot; never crashes the stage on failure."""
    from src.db_client import Database

    try:
        db = Database()
        st.write_status(
            db,
            name,
            status=stats.get("status", "running"),
            processed=stats.get("ok", stats.get("processed", 0)),
            queue_depth=stats.get("queue_depth", 0),
            lag_seconds=stats.get("lag_seconds", 0),
            detail=stats.get("detail", ""),
        )
        db.close()
    except Exception as e:  # status reporting is best-effort
        logger.debug(f"[{name}] status write failed (non-fatal): {e}")


def run_stage(
    name: str,
    cycle_fn: Callable[[], dict],
    has_work: Optional[Callable[[], bool]] = None,
    idle_delay: float = 5.0,
    max_backoff: float = 300.0,
) -> int:
    """Run a stage until SIGTERM/SIGINT. Returns process exit code.

    cycle_fn returns a stats dict used for status reporting. Keys:
      ok/processed   → items processed this cycle
      queue_depth     → backlog
      lag_seconds     → how far behind
      detail          → free-form note
      empty           → True if no work (stage sleeps idle_delay)
      fatal           → True ⇒ return 1 (real crash for supervisor restart)
    """
    global _running
    _running = True
    _install_signals()

    backoff = _StageBackoff(max_delay=max_backoff)
    consecutive_errors = 0
    last_report = 0.0

    while _running:
        try:
            if has_work and not has_work():
                # Idle: report periodically (so the dashboard's "last seen"
                # stays fresh and can tell idle from down), then sleep cheaply.
                now = time.time()
                if now - last_report >= 30.0:
                    _report(name, {"status": "idle", "detail": "no work"})
                    last_report = now
                _interruptible_sleep(idle_delay)
                continue

            stats = cycle_fn() or {}
            backoff.reset()
            consecutive_errors = 0
            last_report = time.time()
            # Map `empty` → idle so an up-to-date stage shows idle (not running)
            # in the dashboard, matching the has_work-idle path. Default the
            # detail to "no work" for consistency with download/parse.
            if stats.get("empty"):
                if "status" not in stats:
                    stats["status"] = "idle"
                if not stats.get("detail"):
                    stats["detail"] = "no work"
            _report(name, stats)

            if stats.get("fatal"):
                logger.error(f"[{name}] fatal error, exiting for supervisor restart")
                return 1

            if stats.get("empty"):
                _interruptible_sleep(idle_delay)
            else:
                _interruptible_sleep(0.2)  # yield; next claim picks up more work

        except KeyboardInterrupt:
            break
        except Exception as e:
            consecutive_errors += 1
            wait = backoff.next_delay()
            logger.error(
                f"[{name}] cycle failed (attempt {consecutive_errors}): {e}; backoff {wait:.0f}s"
            )
            _report(name, {"status": "error", "detail": str(e)[:200]})
            _interruptible_sleep(wait)

    logger.info(f"[{name}] stopped")
    return 0

"""Discovery stage — external-site poller with circuit breaker.

Discovery is the one stage that legitimately polls: PlusForward has no push
mechanism, so we must periodically scan the matchlist for new matches. The
revamp improves it with:

- A circuit breaker on the PlusForward dependency so a site outage backs off
  instead of hammering it every cycle.
- Status reporting so the dashboard shows discovery's backlog/lag.

The scan kernel is the proven `match_discovery.discover_matches`, untouched.
"""

from __future__ import annotations

import logging

from src.backoff import CircuitBreaker
from src.match_discovery import discover_matches

logger = logging.getLogger("pipeline.discovery")

# Module-level breaker so it persists across cycles within the process.
_breaker = CircuitBreaker(name="plusforward", failure_threshold=3, cooldown=30.0)


def run_cycle(max_pages: int = 0, forward_only: bool = False) -> dict:
    """Run one discovery scan. Returns stats dict (or {'tripped': True}).

    Returns `empty: True` when nothing new was found, so the runner applies
    the discovery idle delay instead of hot-looping at 0.2s. This is the key
    guard against hammering PlusForward when the matchlist is caught up.
    """
    if not _breaker.allow_request():
        return {"tripped": True, "new": 0, "empty": True}

    try:
        new = discover_matches(max_pages=max_pages, forward_only=forward_only)
        _breaker.record_success()
        return {"new": new, "empty": new == 0}
    except Exception as e:
        _breaker.record_failure()
        logger.warning(f"discovery failed (breaker={_breaker.state}): {e}")
        return {"tripped": _breaker.state == "open", "new": 0, "empty": True, "error": str(e)[:200]}

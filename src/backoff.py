"""Backoff + circuit-breaker helpers for the pipeline engine.

Two concerns:

1. **Exponential backoff** — a transient failure (site hiccup, ClickHouse blip)
   should not be retried in a tight loop. `Backoff` grows the wait between
   retries: delay * 2^n, capped at `max_delay`, reset on success.

2. **Circuit breaker** — when an external dependency (PlusForward, ClickHouse,
   Discord, Twitch) fails repeatedly, we stop calling it for a cooldown window
   instead of hammering it. After the cooldown we try one probe; success closes
   the circuit, failure re-opens it with a longer cooldown.

This module is pure — it has no ClickHouse or network coupling — so it can be
unit-tested in isolation and reused by every stage.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("pipeline")

# Circuit states
CLOSED = "closed"    # normal: calls allowed
OPEN = "open"        # tripped: calls blocked until cooldown
HALF_OPEN = "half_open"  # probe: one test call allowed


@dataclass
class Backoff:
    """Exponential backoff with jitter and optional cap."""

    base_delay: float = 2.0
    max_delay: float = 300.0     # 5 min cap by default
    factor: float = 2.0
    jitter: float = 0.1          # ±10% jitter to avoid thundering herd

    _attempts: int = field(default=0, init=False)

    def reset(self) -> None:
        """Call after a success to reset the attempt counter."""
        self._attempts = 0

    def next_delay(self) -> float:
        """Return the delay to wait before the next retry (and advance)."""
        self._attempts += 1
        exp = min(self.max_delay, self.base_delay * (self.factor ** (self._attempts - 1)))
        if self.jitter > 0:
            exp *= 1.0 + random.uniform(-self.jitter, self.jitter)
        return round(exp, 3)


@dataclass
class CircuitBreaker:
    """Trips open after `failure_threshold` consecutive failures.

    - CLOSED: normal operation.
    - OPEN:   calls are blocked (raise/return sentinel) until `cooldown` elapses.
    - HALF_OPEN: after cooldown, one probe call is allowed; success → CLOSED,
      failure → OPEN with a fresh (optionally longer) cooldown.
    """

    name: str
    failure_threshold: int = 5
    cooldown: float = 30.0
    cooldown_factor: float = 2.0
    max_cooldown: float = 600.0

    state: str = field(default=CLOSED, init=False)
    _failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _logger: Optional[logging.Logger] = field(default=None, init=False)

    def _log(self, level: int, msg: str) -> None:
        (self._logger or logging.getLogger("pullback")).log(level, msg)

    def allow_request(self) -> bool:
        """Return True if a call may proceed now."""
        now = time.time()
        if self.state == CLOSED:
            return True
        if self.state == HALF_OPEN:
            # Only one probe is allowed; further calls wait until it resolves.
            return False
        # OPEN: if cooldown elapsed, transition to HALF_OPEN and allow the probe.
        if now - self._opened_at >= self.cooldown:
            self.state = HALF_OPEN
            self._log(logging.INFO, f"circuit {self.name} half-open (probing)")
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        if self.state != CLOSED:
            self.state = CLOSED
            self._log(logging.INFO, f"circuit {self.name} closed")

    def record_failure(self) -> None:
        self._failures += 1
        if self.state in (CLOSED, HALF_OPEN) and self._failures >= self.failure_threshold:
            self.state = OPEN
            self._opened_at = time.time()
            self.cooldown = min(self.max_cooldown, self.cooldown * self.cooldown_factor)
            self._log(
                logging.WARNING,
                f"circuit {self.name} OPEN (after {self._failures} failures), "
                f"cooldown {self.cooldown:.0f}s",
            )

    @property
    def failure_count(self) -> int:
        return self._failures

    def state_json(self) -> dict:
        """Serialize for the dashboard health strip."""
        now = time.time()
        return {
            "name": self.name,
            "state": self.state,
            "failures": self._failures,
            "cooldown_remaining": (
                round(self.cooldown - (now - self._opened_at), 1)
                if self.state == OPEN
                else 0.0
            ),
        }

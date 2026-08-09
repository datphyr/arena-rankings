"""Daemon runner — shared daemon loop for all pipeline wrappers.

Provides a single `run_daemon` function that handles:
  - Single-run vs daemon mode (--daemon flag)
  - Configurable delay between cycles
  - Graceful shutdown on SIGINT/SIGTERM (finishes current cycle, then stops)
  - Optional file logging (stdout only by default, for systemd/journalctl)
  - Idle detection (no work → log differently, still sleep)

Usage from wrappers:

    from src.daemon import run_daemon

    def my_cycle(args):
        # do work, return a summary string
        return f"3 new matches"

    run_daemon(
        name="discovery",
        cycle_fn=my_cycle,
        cycle_args=args,
        daemon=args.daemon,
        delay=args.delay,
        log_file=args.log_file,
        verbose=args.verbose,
    )
"""

import logging
import signal
import sys
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("daemon")

_running = True


def _signal_handler(signum, frame):
    global _running
    _running = False
    raise KeyboardInterrupt


def _setup(name: str, verbose: bool):
    """Configure logging to stdout — systemd/journalctl handles rotation."""
    from config import LOG_LEVEL
    if verbose:
        level = logging.DEBUG
    else:
        level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    # Silence noisy third-party library loggers — keep our code at DEBUG.
    # discord.py uses logger name "discord" — same as our stage_logger for the bot
    # component. Only silence discord.py's sub-loggers, not the parent.
    for noisy in ("clickhouse_driver", "discord.http", "discord.gateway",
                  "discord.client", "discord.shard", "discord.webhook",
                  "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("tzlocal").setLevel(logging.WARNING)


def run_daemon(
    name: str,
    cycle_fn: Callable[[Any], str],
    cycle_args: Any,
    daemon: bool = False,
    delay: int = 60,
    verbose: bool = False,
) -> int:
    """Run a pipeline stage as a single run or daemon loop.

    Args:
        name: Stage name for logging (e.g. "discovery", "download").
        cycle_fn: Function called each cycle. Receives cycle_args, returns a summary string.
        cycle_args: Passed to cycle_fn each cycle.
        daemon: If True, loop forever with delay between cycles.
        delay: Seconds between cycles in daemon mode.
        verbose: Enable debug logging.

    Returns:
        Exit code (0 = clean, 1 = error in single mode).
    """
    global _running
    _running = True

    # Install signal handlers only in daemon mode.
    # In single mode, let Ctrl+C raise KeyboardInterrupt immediately.
    if daemon:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

    _setup(name, verbose)

    stage_logger = logging.getLogger(name)

    if not daemon:
        try:
            result = cycle_fn(cycle_args)
            if result:
                stage_logger.info(result)
        except KeyboardInterrupt:
            return 130
        except Exception as e:
            stage_logger.error(f"failed: {e}", exc_info=True)
            return 1
        return 0

    # Daemon mode
    cycle = 0
    while _running:
        cycle += 1

        try:
            result = cycle_fn(cycle_args)
            if result:
                stage_logger.debug(f"cycle: {result}")
        except KeyboardInterrupt:
            break
        except Exception as e:
            stage_logger.error(f"failed: {e}", exc_info=True)

        if not _running:
            break

        try:
            slept = 0
            while slept < delay and _running:
                time.sleep(1)
                slept += 1
        except KeyboardInterrupt:
            break

    return 0
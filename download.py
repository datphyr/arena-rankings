#!/usr/bin/env python3
"""Wrapper for match downloading — batch downloads pending matches from PlusForward.

Usage:
    python download.py                         # single run, 1 worker
    python download.py --daemon                # daemon mode (loop forever)
    python download.py --daemon --delay 30     # custom restart delay
    python download.py --workers 3             # 3 concurrent workers
    python download.py --workers 5 --limit 100 # 5 workers, 100 matches per cycle
    python download.py -v                      # verbose
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import DAEMON_RESTART_DELAY
from src.daemon import run_daemon
from src.match_downloader import download_batch


def cycle(args):
    success, failure = download_batch(workers=args.workers, limit=args.limit)
    return f"{success} success, {failure} failure"


def main():
    parser = argparse.ArgumentParser(description="Match download wrapper")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run in daemon mode")
    parser.add_argument("--delay", type=int, default=DAEMON_RESTART_DELAY, help=f"Seconds between cycles (default: {DAEMON_RESTART_DELAY})")
    parser.add_argument("--workers", "-w", type=int, default=1, help="Concurrent download workers (default: 1)")
    parser.add_argument("--limit", "-l", type=int, default=0, help="Max matches per cycle (0 = unlimited)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    sys.exit(run_daemon(
        name="download",
        cycle_fn=cycle,
        cycle_args=args,
        daemon=args.daemon,
        delay=args.delay,
        verbose=args.verbose,
    ))


if __name__ == "__main__":
    main()
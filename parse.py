#!/usr/bin/env python3
"""Wrapper for match parsing — parses downloaded match HTML into structured ClickHouse data.

Usage:
    python parse.py                        # single run, all cores
    python parse.py --daemon               # daemon mode (loop forever)
    python parse.py --daemon --delay 30    # custom restart delay
    python parse.py --workers 4            # use 4 parser threads
    python parse.py --limit 100            # limit matches per cycle
    python parse.py -v                     # verbose
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import DAEMON_RESTART_DELAY, PARSER_WORKERS
from src.daemon import run_daemon
from src.match_parser import parse_all_matches


def cycle(args):
    success, failure = parse_all_matches(limit=args.limit, workers=args.workers)
    return f"{success} success, {failure} failure"


def main():
    parser = argparse.ArgumentParser(description="Match parsing wrapper")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run in daemon mode")
    parser.add_argument("--delay", type=int, default=DAEMON_RESTART_DELAY, help=f"Seconds between cycles (default: {DAEMON_RESTART_DELAY})")
    parser.add_argument("--workers", "-w", type=int, default=0, help=f"Parser threads (0 = auto, default: {PARSER_WORKERS})")
    parser.add_argument("--limit", "-l", type=int, default=0, help="Max matches per cycle (0 = unlimited)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    sys.exit(run_daemon(
        name="parse",
        cycle_fn=cycle,
        cycle_args=args,
        daemon=args.daemon,
        delay=args.delay,
        verbose=args.verbose,
    ))


if __name__ == "__main__":
    main()
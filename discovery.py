#!/usr/bin/env python3
"""Wrapper for match discovery — scans PlusForward matchlist and registers new matches.

Discovery registers match IDs in raw_posts with status 'discovered' (raw_html
empty, sort_time from the matchlist). The download stage then fetches each and
flips it to 'downloaded'. Only used in discovery download mode.

Usage:
    python discovery.py                        # single run
    python discovery.py --daemon               # daemon mode (loop forever)
    python discovery.py --daemon --delay 30    # custom restart delay
    python discovery.py --max-pages 50          # limit pages per cycle
    python discovery.py --forward-only          # only scan from page 1
    python discovery.py -v                      # verbose
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import DAEMON_RESTART_DELAY
from src.daemon import run_daemon
from src.match_discovery import discover_matches


def cycle(args):
    new = discover_matches(max_pages=args.max_pages, forward_only=args.forward_only)
    return f"{new} new matches registered"


def main():
    parser = argparse.ArgumentParser(description="Match discovery wrapper")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run in daemon mode")
    parser.add_argument("--delay", type=int, default=DAEMON_RESTART_DELAY, help=f"Seconds between cycles (default: {DAEMON_RESTART_DELAY})")
    parser.add_argument("--max-pages", type=int, default=0, help="Max pages per cycle (0 = unlimited)")
    parser.add_argument("--forward-only", action="store_true", help="Only scan forward from page 1")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    sys.exit(run_daemon(
        name="discovery",
        cycle_fn=cycle,
        cycle_args=args,
        daemon=args.daemon,
        delay=args.delay,
        verbose=args.verbose,
    ))


if __name__ == "__main__":
    main()

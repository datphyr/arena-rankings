#!/usr/bin/env python3
"""Wrapper for post downloading — downloads ALL PlusForward posts into raw_posts.

Scans /post/1, /post/2, ... sequentially (no separate discovery stage), storing
each page's HTML in raw_posts with status 'downloaded'. Stops at the post wall
(invalid post id). Sidebar cookies are sent on every request to shrink pages.

Usage:
    python download.py                         # single run
    python download.py --daemon                # daemon mode (loop forever)
    python download.py --daemon --delay 30     # custom restart delay
    python download.py --limit 1000            # limit posts per cycle
    python download.py -v                      # verbose
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


from config import DAEMON_RESTART_DELAY, DOWNLOADER_WORKERS
from src.daemon import run_daemon
from src.post_downloader import download_posts


def cycle(args):
    downloaded, wall_hit, complete = download_posts(limit=args.limit, workers=args.workers)
    if complete:
        return f"{downloaded} downloaded, history complete"
    return f"{downloaded} downloaded (scanning...)"


def main():
    parser = argparse.ArgumentParser(description="Post download wrapper")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run in daemon mode")
    parser.add_argument("--delay", type=int, default=DAEMON_RESTART_DELAY, help=f"Seconds between cycles (default: {DAEMON_RESTART_DELAY})")

    parser.add_argument("--workers", "-w", type=int, default=DOWNLOADER_WORKERS, help=f"Concurrent download workers (default: {DOWNLOADER_WORKERS})")
    parser.add_argument("--limit", "-l", type=int, default=0, help="Max posts per cycle (0 = unlimited)")
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

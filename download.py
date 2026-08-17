#!/usr/bin/env python3
"""Wrapper for the download stage — fetches match/post HTML into raw_posts.

Two modes (see config.DOWNLOAD_MODE):

  discovery (default):
    Matchlist discovery (src.match_discovery) registers match IDs in raw_posts
    with status 'discovered'. This wrapper then fetches each discovered match
    page and stores it as 'downloaded' (ready to parse). It holds off until
    discovery's backward scan is complete so the full history is catalogued
    before processing (ratings stay chronological).

  sequential:
    Scans /post/1, /post/2, ... sequentially (no separate discovery stage),
    storing each page's HTML in raw_posts with status 'downloaded'. Stops at
    the post wall (invalid post id). Sidebar cookies shrink each page.

Usage:
    python download.py                         # single run (default mode)
    python download.py --daemon                # daemon mode (loop forever)
    python download.py --daemon --delay 30     # custom restart delay
    python download.py --limit 1000            # limit posts per cycle
    python download.py -v                      # verbose
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


from config import DAEMON_RESTART_DELAY, DOWNLOADER_WORKERS, DOWNLOAD_MODE
from src.daemon import run_daemon


def cycle(args):
    if DOWNLOAD_MODE == "sequential":
        from src.post_downloader import download_posts
        downloaded, wall_hit, complete = download_posts(limit=args.limit, workers=args.workers)
        if complete:
            return f"{downloaded} downloaded, history complete"
        return f"{downloaded} downloaded (scanning...)"

    # Discovery mode
    from src.db_client import discovery_complete
    from src.match_downloader import download_batch

    if not discovery_complete():
        # Hold off until discovery has scanned back to the oldest match, so we
        # process the full history oldest→newest (ratings stay chronological).
        return "waiting for discovery (backward scan not complete)"
    success, failure = download_batch(workers=args.workers, limit=args.limit)
    return f"{success} success, {failure} failure"


def main():
    parser = argparse.ArgumentParser(description="Download wrapper (discovery or sequential mode)")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run in daemon mode")
    parser.add_argument("--delay", type=int, default=DAEMON_RESTART_DELAY, help=f"Seconds between cycles (default: {DAEMON_RESTART_DELAY})")

    parser.add_argument("--workers", "-w", type=int, default=DOWNLOADER_WORKERS, help=f"Concurrent download workers (default: {DOWNLOADER_WORKERS})")
    parser.add_argument("--limit", "-l", type=int, default=0, help="Max posts per cycle (0 = unlimited)")
    parser.add_argument("--log-file", default=None, help="Optional rotating log file (in addition to stdout)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    sys.exit(run_daemon(
        name="download",
        cycle_fn=cycle,
        cycle_args=args,
        daemon=args.daemon,
        delay=args.delay,
        verbose=args.verbose,
        log_file=args.log_file,
    ))


if __name__ == "__main__":
    main()

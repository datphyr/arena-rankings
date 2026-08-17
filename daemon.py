#!/usr/bin/env python3
"""Arena Rankings pipeline supervisor — runs all components as daemon subprocesses.

Each component runs as a separate process. The supervisor:
  - Starts all components in dependency order (discovery → download → parse → rank → discord)
  - Monitors them and restarts on crash
  - Forwards SIGINT/SIGTERM to all children (graceful shutdown)
  - Parses and reformats child log lines into a unified, aligned format

Output format:
    2026-08-02 21:49:01 INFO  [discovery] Starting forward scan from page 1...
    2026-08-02 21:49:02 INFO  [download]  [1/13005] 94304 ✓ 2026-07-24 16:00:00
    2026-08-02 21:49:03 WARN  [parse]     No match area found in 94284
    2026-08-02 21:49:04 INFO  [rank]      QC: Computing Elo from scratch: 5234 matches
    2026-08-02 21:49:05 INFO  [discord]   Connected to Discord as ArenaBot

Usage:
    python daemon.py                        # run all components
    python daemon.py --no-discord           # skip Discord bot (Twitch bot coming later)
    python daemon.py --workers 3            # download workers
    python daemon.py --delay 60             # restart delay for crashed components
    python daemon.py --restart-delay 0      # override: instant restart (default 0)
    python daemon.py -v                     # verbose

systemd unit (/etc/systemd/system/arena-pipeline.service):
    [Unit]
    Description=Arena Rankings Pipeline
    After=network.target clickhouse-server.service
    Requires=clickhouse-server.service

    [Service]
    Type=simple
    WorkingDirectory=/root/.openclaw/workspace/arena_rankings
    ExecStart=/usr/bin/python3 daemon.py
    Restart=on-failure
    RestartSec=10

    [Install]
    WantedBy=multi-user.target
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import DAEMON_RESTART_DELAY, DOWNLOADER_WORKERS, DOWNLOAD_MODE

logger = logging.getLogger("arena")

# Components in dependency order: (name, script, default args)
# Note: "discord" is the Discord bot — Twitch bot will be added later.
# In discovery mode there's a separate discovery stage (matchlist → raw_posts
# status 'discovered'); in sequential mode download scans /post/N directly.
COMPONENTS = [
    ("discovery", "discovery.py", ["--daemon"]),
    ("download",  "download.py",  ["--daemon"]),
    ("parse",     "parse.py",     ["--daemon"]),
    ("rank",      "rank.py",      ["--daemon"]),
    ("discord",   "bot_discord.py", ["--daemon"]),
    ("twitch",    "bot_twitch.py",  ["--daemon"]),
    ("web",       "api_web.py",     ["--daemon"]),
]


from src.logging_setup import configure_logging


class Supervisor:
    """Manages component subprocesses — start, monitor, restart, shutdown."""

    def __init__(self, components: list[tuple[str, str, list[str]]], restart_delay: int, verbose: bool):
        self.components = components
        self.restart_delay = restart_delay
        self.verbose = verbose
        self.procs: dict[str, subprocess.Popen] = {}
        self.stopping = False

    def _start_one(self, name: str, script: str, args: list[str]) -> subprocess.Popen:
        cmd = [sys.executable, str(Path(__file__).parent / script)] + args
        logger.debug(f"start {name}")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(Path(__file__).parent),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.procs[name] = proc
        return proc

    def start_all(self):
        for name, script, default_args in self.components:
            args = list(default_args)
            if self.verbose and "-v" not in args and "--verbose" not in args:
                args.append("-v")
            self._start_one(name, script, args)
            # Small stagger so they don't all hammer ClickHouse at once
            time.sleep(1)

    def _restart_one(self, name: str):
        script = next(s for n, s, _ in self.components if n == name)
        default_args = next(a for n, s, a in self.components if n == name)
        args = list(default_args)
        if self.verbose and "-v" not in args and "--verbose" not in args:
            args.append("-v")
        logger.warning(f"{name} crashed, restart in {self.restart_delay}s")
        time.sleep(self.restart_delay)
        if self.stopping:
            return
        self._start_one(name, script, args)

    def monitor(self):
        """Main loop — read output lines, check process health, restart crashes."""
        import selectors

        sel = selectors.DefaultSelector()
        for name, proc in self.procs.items():
            sel.register(proc.stdout, selectors.EVENT_READ, data=name)

        while not self.stopping:
            # Check for exited processes
            for name, proc in list(self.procs.items()):
                ret = proc.poll()
                if ret is not None:
                    logger.warning(f"{name} exited (code {ret})")
                    sel.unregister(proc.stdout)
                    del self.procs[name]
                    if not self.stopping:
                        self._restart_one(name)
                        if name in self.procs:
                            sel.register(self.procs[name].stdout, selectors.EVENT_READ, data=name)

            # Read available output lines
            events = sel.select(timeout=1.0)
            for key, _ in events:
                name = key.data
                line = key.fileobj.readline()
                if line:
                    # Children already emit clean, uniformly formatted lines via the
                    # shared logging config — relay them verbatim to journald/stdout.
                    sys.stdout.write(line.decode('utf-8', errors='replace'))
                    sys.stdout.flush()

    def stop_all(self):
        self.stopping = True
        logger.info("shutting down")

        # SIGTERM first (graceful)
        for name, proc in self.procs.items():
            if proc.poll() is None:
                logger.debug(f"stop {name}")
                proc.terminate()

        # Wait up to 15s for graceful exit
        deadline = time.time() + 15
        for name, proc in list(self.procs.items()):
            remaining = max(0, deadline - time.time())
            try:
                    proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                logger.warning(f"{name} unresponsive, killing")
                proc.kill()
                proc.wait()

        self.procs.clear()


def main():
    parser = argparse.ArgumentParser(description="Arena Rankings pipeline supervisor")
    parser.add_argument("--no-discord", action="store_true", help="Skip Discord bot")
    parser.add_argument("--no-twitch", action="store_true", help="Skip Twitch bot")
    parser.add_argument("--no-web", action="store_true", help="Skip web site")
    parser.add_argument("--workers", "-w", type=int, default=DOWNLOADER_WORKERS, help=f"Download workers (default: {DOWNLOADER_WORKERS})")
    parser.add_argument("--delay", type=int, default=DAEMON_RESTART_DELAY, help=f"Restart delay for crashed components (default: {DAEMON_RESTART_DELAY})")
    parser.add_argument("--restart-delay", type=int, default=0, help="Override restart delay for crashed components (default: 0 = instant restart). Takes precedence over --delay.")
    parser.add_argument("--log-file", default=None, help="Optional rotating log file for the whole pipeline (in addition to stdout)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    configure_logging(verbose=args.verbose, log_file=args.log_file)

    # Build component list, applying args
    components = []
    for name, script, default_args in COMPONENTS:
        if name == "discord" and args.no_discord:
            continue
        if name == "twitch" and args.no_twitch:
            continue
        if name == "web" and args.no_web:
            continue
        if name == "discovery" and DOWNLOAD_MODE != "discovery":
            # No separate discovery stage in sequential mode.
            continue
        if name == "download":
            default_args = [f"--workers={args.workers}"] + default_args
        components.append((name, script, default_args))

    supervisor = Supervisor(components, restart_delay=args.restart_delay, verbose=args.verbose)

    # Signal handling
    def handle_signal(signum, frame):
        logger.info(f"signal {signal.Signals(signum).name}")
        supervisor.stop_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    comp_names = ', '.join(n for n, _, _ in components)
    logger.info(f"started: {comp_names}")

    supervisor.start_all()
    logger.info("all components running")

    try:
        supervisor.monitor()
    except KeyboardInterrupt:
        supervisor.stop_all()

    logger.info("stopped")


if __name__ == "__main__":
    main()

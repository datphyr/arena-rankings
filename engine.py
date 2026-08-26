#!/usr/bin/env python3
"""Arena Rankings pipeline orchestrator (event-worker-pool).

Supervises the pipeline stages, each as an isolated process:

    discovery → download → parse → rank

plus the external-facing services (discord, twitch, web) which run as their own
processes and are not part of the scrape/rank hot path.

Unlike the old polling wrappers (removed), each
stage here is an **event consumer**: it only does work when its queue has items
(see engine/runner.py + engine/stages/*). The orchestrator:

  - Starts all stages in dependency order.
  - Monitors and restarts crashed stages (per-stage, preserving isolation).
  - Forwards SIGINT/SIGTERM to all children for graceful shutdown.
  - Relays child logs (already uniformly formatted) to stdout/journald.

systemd: /etc/systemd/system/arena-rankings.service (Type=simple, Restart=on-failure)
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

from config import DAEMON_RESTART_DELAY, DOWNLOAD_MODE
from src.logging_setup import configure_logging

logger = logging.getLogger("arena")

# Stage processes: (name, args). The discord/twitch/web services are long-lived
# socket processes and are not event consumers, but are supervised the same way
# for crash-restart parity with the old daemon.
STAGES = [
    ("discovery", ["discovery"]),
    ("download", ["download"]),
    ("parse", ["parse"]),
    ("rank", ["rank"]),
    ("reconcile", ["reconcile"]),
    ("discord", ["--", "bot_discord.py", "--daemon"]),   # kept as-is
    ("twitch", ["--", "bot_twitch.py", "--daemon"]),      # kept as-is
    ("web", ["--", "api_web.py", "--daemon"]),            # kept as-is
]


class Orchestrator:
    def __init__(self, stages: list[tuple[str, list[str]]], restart_delay: int):
        self.stages = stages
        self.restart_delay = restart_delay
        self.procs: dict[str, subprocess.Popen] = {}
        self.stopping = False

    def _start_one(self, name: str, args: list[str]) -> subprocess.Popen:
        cmd: list[str]
        if args and args[0] == "--":
            # Legacy service wrapper: python <script> --daemon
            cmd = [sys.executable, str(Path(__file__).parent / args[1])] + args[2:]
        else:
            # New stage process: python -m engine.stage <name> [...]
            cmd = [sys.executable, "-m", "engine.stage"] + args
        logger.debug(f"start {name}: {' '.join(cmd)}")
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
        for name, args in self.stages:
            if name == "discovery" and DOWNLOAD_MODE != "discovery":
                continue  # no separate discovery stage in sequential mode
            self._start_one(name, args)
            time.sleep(1)  # stagger so stages don't all start at once

    def _restart_one(self, name: str):
        args = next(a for n, a in self.stages if n == name)
        logger.warning(f"{name} crashed, restart in {self.restart_delay}s")
        time.sleep(self.restart_delay)
        if self.stopping:
            return
        self._start_one(name, args)

    def monitor(self):
        import selectors

        sel = selectors.DefaultSelector()
        for name, proc in self.procs.items():
            sel.register(proc.stdout, selectors.EVENT_READ, data=name)

        while not self.stopping:
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

            events = sel.select(timeout=1.0)
            for key, _ in events:
                name = key.data
                line = key.fileobj.readline()
                if line:
                    sys.stdout.write(line.decode("utf-8", errors="replace"))
                    sys.stdout.flush()

    def stop_all(self):
        self.stopping = True
        logger.info("shutting down")
        for name, proc in self.procs.items():
            if proc.poll() is None:
                proc.terminate()
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
    parser = argparse.ArgumentParser(description="Arena Rankings pipeline orchestrator")
    parser.add_argument("--restart-delay", type=int, default=0,
                        help="Delay before restarting a crashed stage (default 0 = instant)")
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    configure_logging(verbose=args.verbose, log_file=args.log_file)

    orch = Orchestrator(STAGES, restart_delay=args.restart_delay)

    def handle_signal(signum, frame):
        logger.info(f"signal {signal.Signals(signum).name}")
        orch.stop_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    names = ", ".join(n for n, _ in STAGES if not (n == "discovery" and DOWNLOAD_MODE != "discovery"))
    logger.info(f"started: {names}")

    orch.start_all()
    logger.info("all stages running")

    try:
        orch.monitor()
    except KeyboardInterrupt:
        orch.stop_all()

    logger.info("stopped")


if __name__ == "__main__":
    main()

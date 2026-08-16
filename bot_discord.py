#!/usr/bin/env python3
"""Wrapper for Discord bot — arena rankings slash commands.

Usage:
    python bot_discord.py                        # run bot
    python bot_discord.py --daemon                # daemon mode (same behavior, explicit)
    python bot_discord.py --token ***             # explicit token
    python bot_discord.py --verbose               # debug logging

Environment:
    DISCORD_BOT_TOKEN — Discord bot token (required)
"""

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from config import DAEMON_RESTART_DELAY
from src.bot_discord import create_bot
from src.daemon import run_daemon

logger = logging.getLogger("bot_discord")


def cycle(args):
    token = args.token or os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set. Pass --token *** set env var.")

    bot = create_bot(token)
    logger.info("starting")
    # bot.run() is blocking — stays running until killed (auto-reconnects on disconnect)
    # This never returns to the daemon loop, which is fine for a bot
    bot.run()
    return "bot stopped"


def main():
    parser = argparse.ArgumentParser(description="Arena Rankings Discord Bot")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run in daemon mode")
    parser.add_argument("--delay", type=int, default=DAEMON_RESTART_DELAY, help=f"Seconds between cycles (default: {DAEMON_RESTART_DELAY})")
    parser.add_argument("--token", default=None, help="Discord bot token (overrides env)")
    parser.add_argument("--log-file", default=None, help="Optional rotating log file (in addition to stdout)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    sys.exit(run_daemon(
        name="bot",
        cycle_fn=cycle,
        cycle_args=args,
        daemon=args.daemon,
        delay=args.delay,
        verbose=args.verbose,
        log_file=args.log_file,
    ))


if __name__ == "__main__":
    main()
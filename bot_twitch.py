#!/usr/bin/env python3
"""Wrapper for Twitch bot — arena rankings chat commands.

Usage:
    python bot_twitch.py                        # run bot
    python bot_twitch.py --daemon                # daemon mode (same behavior, explicit)
    python bot_twitch.py --token oauth:xxx       # explicit token
    python bot_twitch.py --channel chan1         # single channel
    python bot_twitch.py --channel chan1,chan2   # multiple channels
    python bot_twitch.py --verbose               # debug logging

Environment:
    TWITCH_BOT_TOKEN   — Twitch OAuth token (required, e.g. "oauth:xxxxx")
    TWITCH_CHANNEL     — Channel name(s) to join (required, comma-separated for multiple)
    TWITCH_NICKNAME    — Bot account nickname (default: arenabot)
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from config import DAEMON_RESTART_DELAY
from src.bot_twitch import TwitchBot
from src.daemon import run_daemon

logger = logging.getLogger("bot_twitch")


def cycle(args):
    token = args.token or os.environ.get("TWITCH_BOT_TOKEN")
    channel = args.channel or os.environ.get("TWITCH_CHANNEL")
    nickname = args.nickname or os.environ.get("TWITCH_NICKNAME", "arenabot")

    if not token:
        raise RuntimeError("TWITCH_BOT_TOKEN not set. Pass --token oauth:xxx or set env var.")
    if not channel:
        raise RuntimeError("TWITCH_CHANNEL not set. Pass --channel name or set env var.")

    # Parse comma-separated channels
    channels = [c.strip() for c in channel.split(",") if c.strip()]
    if not channels:
        raise RuntimeError("No valid channels provided.")

    bot = TwitchBot(token=token, channels=channels, nickname=nickname)
    logger.info(f"starting, channels={channels}")
    # bot.run() is blocking asyncio — stays running until killed
    # Auto-reconnects on disconnect, never returns to daemon loop
    asyncio.run(bot.run())
    return "bot stopped"


def main():
    parser = argparse.ArgumentParser(description="Arena Rankings Twitch Bot")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run in daemon mode")
    parser.add_argument("--delay", type=int, default=DAEMON_RESTART_DELAY, help=f"Seconds between cycles (default: {DAEMON_RESTART_DELAY})")
    parser.add_argument("--token", default=None, help="Twitch OAuth token (overrides env)")
    parser.add_argument("--channel", default=None, help="Twitch channel(s) to join, comma-separated (overrides env)")
    parser.add_argument("--nickname", default=None, help="Bot nickname (overrides env, default: arenabot)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    sys.exit(run_daemon(
        name="bot",
        cycle_fn=cycle,
        cycle_args=args,
        daemon=args.daemon,
        delay=args.delay,
        verbose=args.verbose,
    ))


if __name__ == "__main__":
    main()
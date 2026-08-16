#!/usr/bin/env python3
"""Wrapper for the Arena Rankings web site (FastAPI + uvicorn).

Usage:
    python api_web.py                    # run web server (foreground)
    python api_web.py --daemon           # daemon mode (restart on crash)
    python api_web.py --port 8080        # custom port
    python api_web.py --host 0.0.0.0     # bind address

Environment:
    WEB_PORT   — port to bind (default: 8080)
    WEB_HOST   — host to bind (default: 0.0.0.0)
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
from src.daemon import run_daemon

logger = logging.getLogger("api_web")


def cycle(args):
    """Run the uvicorn server. Blocks until the server stops."""
    import uvicorn
    from src.api_web import app

    host = args.host or os.environ.get("WEB_HOST", "0.0.0.0")
    port = args.port or int(os.environ.get("WEB_PORT", "8080"))
    logger.info(f"web server on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return "web server stopped"


def main():
    parser = argparse.ArgumentParser(description="Arena Rankings web site")
    parser.add_argument("--daemon", action="store_true", help="daemon mode (restart on crash)")
    parser.add_argument("--host", default=None, help="bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default: 8080)")
    parser.add_argument("--log-file", default=None, help="Optional rotating log file (in addition to stdout)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    return run_daemon(
        name="web",
        cycle_fn=cycle,
        cycle_args=args,
        daemon=args.daemon,
        delay=DAEMON_RESTART_DELAY,
        verbose=args.verbose,
        log_file=args.log_file,
    )


if __name__ == "__main__":
    sys.exit(main())

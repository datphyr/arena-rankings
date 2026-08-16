"""Central logging configuration for the Arena Rankings pipeline.

Single source of truth for how every entry point logs:
  - stdout handler (for systemd/journalctl) always on
  - optional rotating file handler (logs/arena.log) when a file is requested
  - one shared format and level
  - centralized suppression of noisy third-party library loggers

Usage from any wrapper / script:

    from src.logging_setup import configure_logging

    configure_logging(verbose=args.verbose, log_file=args.log_file)

This should be the ONLY place that calls logging.basicConfig or installs
handlers. Entry points must call it before doing any work.
"""

import logging
import logging.handlers
import os
import sys
from pathlib import Path

# Map internal logger names to short component tags for clean prefixes.
# Anything not listed falls back to its own (shortened) name.
COMPONENT_MAP = {
    "arena": "super",
    "daemon": "super",
    "src.daemon": "super",
    "download": "download",
    "src.post_downloader": "download",
    "post_downloader": "download",
    "parse": "parse",
    "src.match_parser": "parse",
    "match_parser": "parse",
    "src.tournament_resolver": "parse",
    "tournament_resolver": "parse",
    "rank": "rank",
    "rankings": "rank",
    "src.rankings_compute": "rank",
    "rankings_compute": "rank",
    "discord": "discord",
    "bot_discord": "discord",
    "src.bot_discord": "discord",
    "twitch": "twitch",
    "bot_twitch": "twitch",
    "src.bot_twitch": "twitch",
    "web": "web",
    "api_web": "web",
    "src.api_web": "web",
    "uvicorn": "web",
    "uvicorn.access": "web",
    "uvicorn.error": "web",
    "fetch": "fetch",
    "fetcher": "fetch",
    "src.bracket_fetcher": "fetch",
    "bracket_fetcher": "fetch",
}

# Third-party loggers that spam at DEBUG/INFO — keep at WARNING.
NOISY_LOGGERS = (
    "clickhouse_driver",
    "discord.http",
    "discord.gateway",
    "discord.client",
    "discord.shard",
    "discord.webhook",
    "urllib3",
    "asyncio",
    # tzlocal spams DEBUG lines about /etc/localtime on every import
    "tzlocal",
)

_FORMAT = "%(asctime)s %(levelname)s [%(component)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


class _ComponentFormatter(logging.Formatter):
    """Formatter that replaces the logger name with a short component tag."""

    def __init__(self, fmt=None, datefmt=None):
        super().__init__(fmt or _FORMAT, datefmt or _DATEFMT)

    def format(self, record):
        name = record.name or ""
        record.component = COMPONENT_MAP.get(name, name.rsplit(".", 1)[-1])
        return super().format(record)


def default_log_file() -> Path:
    """Default log path: <repo>/logs/arena.log."""
    return Path(__file__).resolve().parent.parent / "logs" / "arena.log"


def _resolve_level(verbose: bool) -> int:
    if verbose:
        return logging.DEBUG
    env = os.environ.get("LOG_LEVEL", "DEBUG")
    return getattr(logging, env.upper(), logging.DEBUG)


def configure_logging(
    verbose: bool = False,
    log_file: str | os.PathLike | None = None,
    log_level: int | str | None = None,
) -> None:
    """Install the shared logging config.

    Idempotent: safe to call multiple times (e.g. wrapper + module both call it).

    Args:
        verbose: Force DEBUG level (overrides log_level/LOG_LEVEL).
        log_file: Optional path to a rotating log file. If omitted, uses
            LOG_FILE/LOG_DIR env vars; if none set, stdout-only (journald).
        log_level: Optional explicit level (int or "INFO"-style string).
            Overrides LOG_LEVEL env. Ignored if verbose=True.
    """
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # handlers gate actual output

    if log_level is not None and not verbose:
        if isinstance(log_level, str):
            level = getattr(logging, log_level.upper(), logging.DEBUG)
        else:
            level = int(log_level)
    else:
        level = _resolve_level(verbose)

    formatter = _ComponentFormatter()

    # stdout handler — always on (systemd/journalctl / terminal)
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setLevel(level)
    stdout.setFormatter(formatter)
    root.addHandler(stdout)

    # optional rotating file handler
    file_path = log_file
    if file_path is None:
        file_env = os.environ.get("LOG_FILE")
        if file_env:
            file_path = file_env
        else:
            log_dir = os.environ.get("LOG_DIR")
            if log_dir:
                file_path = Path(log_dir) / "arena.log"

    if file_path:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Suppress noisy third-party loggers.
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def quiet_library(name: str, level: int = logging.WARNING) -> None:
    """Set a specific library logger's level (e.g. uvicorn access logs)."""
    logging.getLogger(name).setLevel(level)

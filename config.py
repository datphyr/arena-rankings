"""Configuration for Arena Rankings System."""

import os

# Log level: DEBUG, INFO, WARNING, ERROR (default INFO)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG")

# Daemon mode
DAEMON_RESTART_DELAY = int(os.environ.get("DAEMON_RESTART_DELAY", "60"))

# ClickHouse
CLICKHOUSE_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CLICKHOUSE_PORT = int(os.environ.get("CLICKHOUSE_PORT", "9000"))
CLICKHOUSE_DATABASE = os.environ.get("CLICKHOUSE_DATABASE", "arena_rankings")
CLICKHOUSE_USER = os.environ.get("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "quakepass")

# Scraping — single timeout for all HTTP requests (connect + read).
# curl uses this for both --connect-timeout and --max-time.
# Working pages respond in ~0.2s; stalled pages need ~3s but data arrives fast.
BASE_URL = "https://www.plusforward.net"
MATCHLIST_URL = f"{BASE_URL}/matchlist/results/"
RATE_LIMIT_DELAY = float(os.environ.get("RATE_LIMIT_DELAY", "0.0"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "3"))
RETRY_BACKOFF = 2.0

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/119.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# Downloader workers — network limited, single-threaded by default
DOWNLOADER_WORKERS = int(os.environ.get("DOWNLOADER_WORKERS", "1"))

# Parser workers — default: use all CPU cores
# Set to 1 for single-threaded (debugging), or N to limit.
PARSER_WORKERS = int(os.environ.get("PARSER_WORKERS", str(os.cpu_count() or 1)))

# Leaderboard minimum matches (filters provisional players)
MIN_MATCHES_ELO = int(os.environ.get("MIN_MATCHES_ELO", "0"))
MIN_MATCHES_GLICKO2 = int(os.environ.get("MIN_MATCHES_GLICKO2", "30"))

# Elo K-factor: experience-based base × tournament tier multiplier
# Base K-factor by player experience (games played)
ELO_K_BASE = {
    "provisional": 40,   # <30 games: new player, uncertain rating
    "established": 24,   # 30-100 games: standard
    "veteran": 16,       # >100 games: stable rating
}
# Tier multiplier applied on top of base K
ELO_TIER_MULTIPLIER = {
    "premier": 2.0,
    "major": 1.5,
    "minor": 1.0,
}
DEFAULT_TIER_MULTIPLIER = 1.0

# Glicko-2 rating period: 'year', 'month', 'week', or 'day'
# Glicko recommends 10-15 games per player per period; yearly gives the most
# matches per period for our data volume (most players have very few matches).
GLICKO2_PERIOD = os.environ.get("GLICKO2_PERIOD", "month")

# Glicko-2 system constant tau (constrains volatility change per period).
# Range: 0.2 (stable) to 1.2 (volatile). Default 0.5.
GLICKO2_TAU = float(os.environ.get("GLICKO2_TAU", "1.2"))

# Glicko-2 initial volatility.
GLICKO2_INITIAL_VOL = float(os.environ.get("GLICKO2_INITIAL_VOL", "0.06"))

# Discord Bot
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

# Twitch Bot
TWITCH_BOT_TOKEN = os.environ.get("TWITCH_BOT_TOKEN", "")
TWITCH_CHANNEL = os.environ.get("TWITCH_CHANNEL", "")
TWITCH_NICKNAME = os.environ.get("TWITCH_NICKNAME", "arenabot")

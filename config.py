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

# PlusForward anonymous settings cookie (pf_anon_settings) with both sidebars
# and all sidebar boxes (streams/activity/calendar/matches) disabled. This makes
# the server omit the sidebar HTML entirely, roughly halving each page's size.
# Obtained once via POST /settings/ with anon_updatesettings=1 (no account
# needed). Override via PF_ANON_SETTINGS_COOKIE if you want different prefs.
PF_ANON_SETTINGS_COOKIE = os.environ.get(
    "PF_ANON_SETTINGS_COOKIE",
    "%7B%22tz%22%3A%22UTC%22%2C%22cindents%22%3A0%2C%22theme%22%3A%22default%22%2C%22featured%22%3A1%2C%22topbar%22%3A%22default%22%2C%22sidebar%22%3A%7B%22left%22%3A%7B%22enabled%22%3A0%2C%22boxes%22%3A%5B%5D%7D%2C%22right%22%3A%7B%22enabled%22%3A0%2C%22boxes%22%3A%5B%5D%7D%7D%7D",
)
# Cookie header sent on every PlusForward request (accepts cookies + disables
# sidebars). Empty string = no cookie header.
PF_COOKIE_HEADER = os.environ.get(
    "PF_COOKIE_HEADER",
    f"pf_cookiewarning=1; pf_anon_settings={PF_ANON_SETTINGS_COOKIE}",
)

# Downloader workers — concurrent post downloads. Measured: ~5/s at 1 worker,
# ~38/s at 8 workers, ~34/s at 16 (network-bound plateau). 8 is a good balance
# of throughput vs. rate-limit risk on PlusForward.
DOWNLOADER_WORKERS = int(os.environ.get("DOWNLOADER_WORKERS", "1"))

# Wall detection — consecutive invalid posts (generic "Post | Plus Forward"
# title) that must appear before we treat it as the end of the sequence (the
# wall) rather than a run of deleted posts. The largest deleted block observed
# is ~85 posts, so 250 is a safe margin: it bypasses any deleted block without
# false positives. Set lower to detect the wall sooner, higher to tolerate
# larger deleted blocks.
WALL_CONSECUTIVE = int(os.environ.get("WALL_CONSECUTIVE", "100"))

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

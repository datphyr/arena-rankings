ARENA RANKINGS SYSTEM

This project generates player rankings (Elo / Glicko-2) from plusforward.net match data.

Current status: **partially implemented** — discovery, download, parsing, ranking,
CLI, Discord bot, and Twitch bot are working. Web API is planned but not started.

Data as of 2026-08-04:
- 71,462 matches discovered, 54,390 parsed (1v1 duels only)
- 5,443 players, 1,982 tournaments, 76 countries
- Match data from 1999-08-08 to 2026-08-02
- 12 games with match data (Quake Champions, Quake Live, Diabotical most active)


General pipeline:
1. Download matches in raw format and store them in ClickHouse.
2. Parse downloaded matches into usable data and store in ClickHouse.
3. Compute rankings from parsed data and store in ClickHouse.
4. Expose this data via Discord bot / Twitch bot / Web API / CLI.


Detailed info for each step:

1. MATCH DISCOVERY + DOWNLOADING (implemented ✓)
1.1 MATCH DISCOVERY — src/match_discovery.py
1.2 MATCH DOWNLOAD — src/match_download.py + src/match_downloader.py
- Scrapes https://www.plusforward.net/matchlist/results/?page=N pages.
- Extracts match IDs and timestamps from `listmatch` entries.
- Stores IDs in ClickHouse `match_registry` table with status='discovered'.
- Resume strategy:
  - Phase 1 (forward): always start from page 1, scan forward until all matches
    on a page are already known (catches new matches).
  - Phase 2 (backward): continue from last_known_page+1 to find older matches.
  - `backward_complete` flag in discovery_state prevents re-scanning old pages.
- Wrapper: discovery.py (daemon mode supported)

1.2.1 match_download.py
- Downloads a single match page by ID, stores raw HTML in match_registry.
- Uses curl --compressed (server has broken chunked gzip — curl handles it,
  Python requests/urllib3 hangs).
- Status transitions: discovered → downloaded → parsed/failed.
1.2.2 match_downloader.py
- Batch downloads all pending matches (status='discovered').
- ThreadPoolExecutor for concurrent downloads (default: 1 worker, network-limited).
- Progress logging at 10% intervals.
- Wrapper: download.py (daemon mode, --workers N)

2. MATCH PARSING (implemented ✓) — src/match_parser.py
- Parses raw HTML from match_registry into structured ClickHouse tables:
  players, tournaments, matches, match_maps.
- Only parses 1v1 duel matches (skips TDM, CTF, team formats).
- MatchDetail dataclass captures: players, scores, winner, game, tournament,
  stage, format, maps, played_at, status.
- Tier resolution: src/tier_resolver.py fetches tournament pages and extracts
  tier (premier/major/minor) from the page HTML using title heuristics.
  Caches in DB + memory.
  - Premier/major: keyword ("Premier"/"Major") in the inner title text.
  - Minor: inner title exists but no tier keyword.
  - Non-standard pages (no postinnercontent) default to minor.
  - Network fetches use infinite retries, checking DB between attempts
    (another worker may resolve the same tournament concurrently).
- Multithreaded parsing (default: all CPU cores). Each worker has own DB
  connection, fetcher, and tier resolver. Tournament tiers pre-loaded from DB.
- Wrapper: parse.py (daemon mode)

3. RANKINGS COMPUTATION (implemented ✓) — src/rankings_compute.py
- Two rating systems:
  3.1 Elo — variable K-factor based on player experience × tournament tier:
    - Provisional (<30 games): K=40
    - Established (30-100 games): K=24
    - Veteran (>100 games): K=16
    - Tier multiplier: premier ×2.0, major ×1.5, minor ×1.0
  3.2 Glicko-2 — period-based rating with RD (rating deviation) and volatility:
    - Period: configurable (year/month/week/day), default: month
    - Tau (volatility constraint): 1.2
    - Initial: rating=1500, RD=350, vol=0.06
    - Inactive player RD increases between periods.
    - Sorts by rating - RD (conservative lower bound) for leaderboards.
- Incremental computation:
  - Compares match_id min/max in matches table vs rating_history.
  - 'up_to_date': skip. 'new_matches': incremental update. 'backfill': full recompute.
  - Glicko-2 reloads from start of last period to catch late-added matches.
- Stores ratings in player_ratings, history snapshots in rating_history.
- Rating history tracks peak rating + peak date per player per game.
- Wrapper: rank.py (daemon mode, computes per-game + combined for both systems)

4. DATA EXPOSURE
4.0 SHARED LAYER (implemented ✓) — src/data_provider.py
- DataProvider class: common queries for all consumers.
- Queries: get_top_players, get_top_players_by_peak, get_top_players_asof,
  get_player_ratings, get_player_history, get_h2h, get_matches,
  get_player_matches, get_stats, get_games, get_tournaments, get_tournament_stats,
  get_player_rank.
- Shared column definitions (Col builders) and game constants (ALL_GAMES,
  GAME_ALIASES) for consistent formatting across consumers.
- src/table.py: print_table / table_lines for aligned text table rendering.
- ASOF queries: reconstruct leaderboards at any point in time from rating_history.

4.1 CLI (implemented ✓) — cli.py
- Subcommands: top, player, history, h2h, matches, player-matches, stats,
  games, tournaments.
- Filters: --game, --system (elo/glicko2), --limit, --min-matches, --date,
  --sort (rating/peak).
- Game aliases: ql, qc, q3, cpm, q4, qw, etc.

4.2 DISCORD BOT (implemented ✓) — src/bot_discord.py + bot_discord.py
- discord.py 2.x with slash commands (app_commands).
- Commands: /top, /player, /history, /h2h, /matches, /player-matches,
  /stats, /games.
- Uses shared fmt_* formatter functions from cli.py via DataProvider
  directly (no subprocess, single persistent DB connection).
- Debounce: 5s per user per command (returns cached result).
- Safe defer/followup with retries for WebSocket stability.

4.3 TWITCH BOT (implemented ✓) — src/bot_twitch.py + bot_twitch.py
- Raw IRC over TLS (no external deps) to irc.chat.twitch.tv:6697.
- Commands: !top, !player, !history, !h2h, !matches, !pmatches, !stats,
  !games, !rank, !help.
- Uses DataProvider layer directly (not subprocess) for low latency in chat.
- Output: compact one-line summaries (no tables/code blocks, Twitch chat limit).
- Auto-reconnects on disconnect with 10s backoff.
- Command prefix: ! (standard for Twitch).

4.4 WEB API (not started) — src/api_web.py + api_web.py
- Planned: REST API exposing DataProvider queries.
- Will serve JSON for external consumers.


INFRASTRUCTURE

HTTP Fetching — src/fetcher.py
- Unified PageFetcher using curl --compressed.
- Rotating User-Agents, rate limiting, exponential backoff with jitter.
- Handles PlusForward's broken chunked gzip (curl rc=28 = partial data OK).
- Infinite retries by default. Tier resolver also uses infinite retries
  via its own loop with DB checks between attempts.

Database — src/db_client.py + src/db_schema.py
- ClickHouse with ReplacingMergeTree for idempotent inserts.
- Tables: match_registry, players, tournaments, matches, match_maps,
  player_ratings, rating_history, discovery_state.
- Schema in src/db_schema.py.

Daemon Supervisor — daemon.py
- Runs all components as separate subprocesses in dependency order:
  discovery → download → parse → rank → discord → twitch.
- Monitors and restarts crashed components after configurable delay.
- Reformats child log lines into unified format with component tags.
- Signal handling: SIGINT/SIGTERM → graceful shutdown (15s timeout → kill).
- systemd unit: arena-rankings.service (After=docker.service for ClickHouse).

Reset Utility — reset.py
- `reset.py rankings` — clear ratings + history (daemon recomputes).
- `reset.py parsed` — clear parsed data, reset match status to discovered.
- `reset.py all` — full reset (drop + recreate + restore downloaded data).
- `reset.py all --no-restore` — full reset to empty database.
- Stops arena-rankings.service before reset, restarts after.

Config — config.py
- All settings via environment variables with defaults.
- ClickHouse: host, port, database, user, password.
- Scraping: base URL, rate limit, HTTP timeout, retry backoff, user agents.
- Downloader: 1 worker (network-limited). Parser: all CPU cores.
- Elo: K-factor tiers, tier multipliers. Glicko-2: period, tau, initial vol.
- Discord: bot token from env.
- Twitch: bot token, channel, nickname from env.
- Min matches: Elo=0, Glicko-2=30 (filters provisional players).


WRAPPERS

Each wrapper can run in daemon mode (--daemon flag): does its work, then
restarts after DAEMON_RESTART_DELAY (default 60s).

├── discovery.py    — wrapper to match discovery
├── download.py     — wrapper to match download
├── parse.py        — wrapper to match parser
├── rank.py         — wrapper to rankings computing
├── bot_discord.py  — wrapper to discord bot
├── bot_twitch.py   — wrapper to twitch bot
├── api_web.py      — wrapper to web API (planned)
├── cli.py          — command line interface
├── daemon.py       — pipeline supervisor (runs all wrappers)
├── reset.py        — database reset utility
└── config.py       — configuration


File structure:
├── PROJECT.md              — this file
├── config.py               — config for project
├── daemon.py               — pipeline supervisor
├── reset.py                — database reset utility
├── discovery.py            — wrapper to match discovery
├── download.py             — wrapper to match download
├── parse.py                — wrapper to match parser
├── rank.py                 — wrapper to rankings computing
├── bot_discord.py          — wrapper to discord bot
├── bot_twitch.py           — wrapper to twitch bot
├── api_web.py              — wrapper to web API (planned)
├── cli.py                  — command line interface
├── arena-rankings.service  — systemd unit
├── .env                    — environment variables
├── .env.example            — example env file
│
├── src/
│   ├── __init__.py
│   ├── daemon.py           — daemon wrapper logic for wrappers
│   ├── db_client.py        — clickhouse client
│   ├── db_schema.py        — clickhouse schema
│   ├── fetcher.py          — unified HTTP fetcher (curl-based)
│   ├── table.py            — shared table formatting (Col, print_table)
│   ├── tier_resolver.py    — tournament tier resolution from PlusForward
│   ├── match_discovery.py  — matchlist scraping + ID extraction
│   ├── match_download.py   — single match download
│   ├── match_downloader.py — batch download with worker pool
│   ├── match_parser.py     — HTML parsing into structured data
│   ├── rankings_compute.py — Elo + Glicko-2 computation
│   ├── data_provider.py      — shared query layer for all consumers
│   ├── bot_discord.py      — discord bot (slash commands)
│   ├── bot_twitch.py       — twitch bot (IRC over TLS, one-line output)
│   ├── api_web.py          — web API (planned)
│   └── cli.py              — CLI + shared fmt_* formatters (used by Discord bot)
# Arena Rankings

Generates player rankings (Elo / Glicko-2) from [plusforward.net](https://www.plusforward.net) match data.

**Status: fully implemented** — discovery, download, parsing, ranking, CLI, Discord bot, Twitch bot, and a full-featured web app are all working.

## Data (as of 2026-08-10)

- 71,544 matches discovered, 54,454 parsed (1v1 duels only)
- 5,447 players, 12 games with match data
- Match data from 1999-08-08 to 2026-08-09
- Most active: Quake Champions (25,282), Quake Live (12,500), Diabotical (6,614)

## Pipeline

1. **Discovery** — scrape match IDs from PlusForward matchlist pages
2. **Download** — fetch raw match pages and store them
3. **Parse** — turn raw HTML into structured data (1v1 duels only)
4. **Rank** — compute Elo / Glicko-2 ratings
5. **Expose** — serve via Discord bot / Twitch bot / Web API / CLI

## Components

| Component | Status | Source |
|-----------|--------|--------|
| Match discovery | ✅ | `src/match_discovery.py` |
| Match download | ✅ | `src/match_download.py`, `src/match_downloader.py` |
| Match parsing | ✅ | `src/match_parser.py` |
| Rankings (Elo + Glicko-2) | ✅ | `src/rankings_compute.py` |
| Data provider (shared query layer) | ✅ | `src/data_provider.py` |
| CLI | ✅ | `cli.py` |
| Discord bot | ✅ | `src/bot_discord.py` |
| Twitch bot | ✅ | `src/bot_twitch.py` |
| Web app | ✅ | `src/api_web.py` + `src/web_templates/` + `src/web_static/` |

## Web app

A full-featured web UI (FastAPI + Jinja2 + vanilla JS + Chart.js) served on port 8080. All pages render server-side with AJAX filter/sort updates and a shared `DataProvider` query layer.

**Pages:**
- **Home** (`/`) — top players, most active, games, recent matches; auto-sized columns
- **Leaderboard** (`/leaderboard`) — Elo/Glicko-2 rankings, game/system/date/limit filters, peak rating + date, RD column
- **Player** (`/player/{id}/{name}`) — rating history chart, Elo + Glicko-2 rating tables, top rivals, recent matches
- **Matches** (`/matches`) — filterable by game/player/tournament/tier, sortable, paginated
- **Tournaments** (`/tournaments`) — tier breakdown summary + all-tournaments table; tier/tournament names link to filtered matches
- **H2H** (`/h2h`) — head-to-head match history between two players
- **JSON API** (`/api/*`) — stats, games, leaderboard, player, player history, matches, tournaments, h2h

**Features:**
- Single JetBrains Mono font site-wide (including Chart.js)
- Day/night theme toggle with gradients and transparency
- Auto column sizing on home/player/matches tables; stable fixed widths (full-dataset) on leaderboard/tournaments so columns don't jump across filters/sorts
- AJAX auto-submit filters (selects + date input), autocomplete on player/tournament inputs
- Sortable tables with reserved arrow space, rank plaques with glow, country flags, avatar gradients
- Tier tags (premier/major/minor) as colored pills

## Rating systems

**Elo** — variable K-factor based on experience × tournament tier:
- Provisional (<30 games): K=40 · Established (30–100): K=24 · Veteran (>100): K=16
- Tier multiplier: premier ×2.0, major ×1.5, minor ×1.0

**Glicko-2** — period-based rating with RD (rating deviation) and volatility:
- Period configurable (year/month/week/day), default month · Tau 1.2
- Initial: rating=1500, RD=350, vol=0.06
- Leaderboards sort by `rating - RD` (conservative lower bound)

## CLI

```
cli.py top [--game ql] [--system elo|glicko2] [--limit N] [--min-matches N] [--date YYYY-MM-DD] [--sort rating|peak]
cli.py player <name> [--min-matches N]
cli.py history <name> [--game ql] [--system elo|glicko2] [--limit N]
cli.py h2h <player1> <player2> [--game ql] [--limit N]
cli.py matches [--game ql] [--limit N]
cli.py player-matches <name> [--game ql] [--limit N]
cli.py stats
cli.py games
cli.py tournaments [--tier premier|major|minor] [--game ql] [--limit N]
```

Game aliases: `ql`, `qc`, `q3`, `cpm`, `q4`, `qw`, etc.

## Bots

**Discord** — slash commands: `/top`, `/player`, `/history`, `/h2h`, `/matches`, `/player-matches`, `/stats`, `/games`. Uses the shared `DataProvider` layer with a single persistent DB connection; 5s debounce per user per command.

**Twitch** — raw IRC over TLS (no external deps), commands: `!top`, `!player`, `!history`, `!h2h`, `!matches`, `!pmatches`, `!stats`, `!games`, `!rank`, `!help`. Compact one-line output (Twitch chat limit), auto-reconnects with 10s backoff.

## Infrastructure

- **Database** — ClickHouse with `ReplacingMergeTree` for idempotent inserts. Tables: `match_registry`, `players`, `tournaments`, `matches`, `match_maps`, `player_ratings`, `rating_history`, `discovery_state`. Schema in `src/db_schema.py`.
- **HTTP fetching** — unified `PageFetcher` using `curl --compressed` (handles PlusForward's broken chunked gzip), rotating user-agents, rate limiting, exponential backoff with jitter.
- **Daemon supervisor** — `daemon.py` runs all components as subprocesses in dependency order (discovery → download → parse → rank → discord → twitch), monitors and restarts crashed components. systemd unit: `arena-rankings.service`.
- **Reset utility** — `reset.py rankings|parsed|all` clears ratings, parsed data, or the whole database (stops the service first, restarts after).

## Configuration

All settings via environment variables with defaults (see `config.py`; `.env.example` documents the secrets):
- ClickHouse: host, port, database, user, password
- Scraping: `RATE_LIMIT_DELAY` (default 0.0), `HTTP_TIMEOUT` (default 3s)
- Downloader: `DOWNLOADER_WORKERS` (default 1, network-limited) · Parser: `PARSER_WORKERS` (default all CPU cores)
- Elo: K-factor tiers, tier multipliers · Glicko-2: `GLICKO2_PERIOD` (month), `GLICKO2_TAU` (1.2), `GLICKO2_INITIAL_VOL` (0.06)
- Discord / Twitch: bot tokens from env
- Min matches: `MIN_MATCHES_ELO`=0, `MIN_MATCHES_GLICKO2`=30 (filters provisional players)

Note: `RETRY_BACKOFF` and `USER_AGENTS` are hardcoded in `config.py` (not env-configurable).

## File structure

```
├── PROJECT.md              — detailed design doc
├── config.py               — config for project
├── daemon.py               — pipeline supervisor
├── reset.py                — database reset utility
├── discovery.py            — wrapper to match discovery
├── download.py             — wrapper to match download
├── parse.py                — wrapper to match parser
├── rank.py                 — wrapper to rankings computing
├── bot_discord.py          — wrapper to discord bot
├── bot_twitch.py           — wrapper to twitch bot
├── cli.py                  — command line interface
├── arena-rankings.service  — systemd unit
├── .env.example            — example env file
│
└── src/
    ├── db_client.py        — clickhouse client
    ├── db_schema.py        — clickhouse schema
    ├── fetcher.py          — unified HTTP fetcher (curl-based)
    ├── table.py            — shared table formatting (Col, print_table)
    ├── tier_resolver.py    — tournament tier resolution from PlusForward
    ├── match_discovery.py  — matchlist scraping + ID extraction
    ├── match_download.py   — single match download
    ├── match_downloader.py — batch download with worker pool
    ├── match_parser.py     — HTML parsing into structured data
    ├── rankings_compute.py — Elo + Glicko-2 computation
    ├── data_provider.py    — shared query layer for all consumers
    ├── bot_discord.py      — discord bot (slash commands)
    ├── bot_twitch.py       — twitch bot (IRC over TLS, one-line output)
    ├── api_web.py          — FastAPI web app (pages + JSON API)
    ├── cli.py              — CLI + shared fmt_* formatters (used by Discord bot)
    │
    ├── web_templates/      — Jinja2 templates (base, home, leaderboard, player,
    │                        matches, tournaments, h2h + shared partials)
    └── web_static/         — static assets (style.css, theme.js, tables.js,
                             ajax-filters.js, autocomplete.js, sort-scroll.js,
                             chart.umd.min.js)
```

## License

See `LICENSE` (if present).

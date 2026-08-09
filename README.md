# Arena Rankings

Generates player rankings (Elo / Glicko-2) from [plusforward.net](https://www.plusforward.net) match data.

**Status: partially implemented** — discovery, download, parsing, ranking, CLI, Discord bot, and Twitch bot are working. Web API is planned but not started.

## Data (as of 2026-08-04)

- 71,462 matches discovered, 54,390 parsed (1v1 duels only)
- 5,443 players, 1,982 tournaments, 76 countries
- Match data from 1999-08-08 to 2026-08-02
- 12 games with match data (Quake Champions, Quake Live, Diabotical most active)

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
| Web API | ⏳ planned | `src/api_web.py` |

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
cli.py top [--game ql] [--system elo|glicko2] [--limit N] [--min-matches N]
cli.py player <name> [--game ql] [--system elo|glicko2]
cli.py history <name> [--game ql] [--system elo|glicko2]
cli.py h2h <player1> <player2> [--game ql]
cli.py matches [--game ql] [--limit N]
cli.py player-matches <name> [--game ql] [--limit N]
cli.py stats [--game ql]
cli.py games
cli.py tournaments [--game ql]
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

All settings via environment variables with defaults (see `.env.example`):
- ClickHouse: host, port, database, user, password
- Scraping: base URL, rate limit, HTTP timeout, retry backoff, user agents
- Downloader: 1 worker (network-limited) · Parser: all CPU cores
- Elo: K-factor tiers, tier multipliers · Glicko-2: period, tau, initial vol
- Discord / Twitch: bot tokens from env
- Min matches: Elo=0, Glicko-2=30 (filters provisional players)

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
    ├── api_web.py          — web API (planned)
    └── cli.py              — CLI + shared fmt_* formatters (used by Discord bot)
```

## License

See `LICENSE` (if present).

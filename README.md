# Arena Rankings

Automated esports player rankings for competitive arena shooters. The project downloads match/tournament pages from [PlusForward](https://www.plusforward.net) into a central raw store, parses them, and computes **Elo** and **Glicko-2** ratings that are served through a web site, a JSON API, and Discord/Twitch chat bots. It is game-agnostic and tracks multiple titles side by side (e.g. Quake Champions, Quake Live, Quake 3 Arena, Quake 3 CPMA, Quake 4, Quake World), each with its own leaderboards and ratings.

## Overview

Arena Rankings is a full data pipeline + front-end for competitive player ratings:

1. **Discover** (discovery mode) — scans the PlusForward matchlist to find match IDs, registering them in `raw_posts` with status `discovered` (and a `sort_time` chronological key from the matchlist).
2. **Download** — fetches match/post HTML into the `raw_posts` table (status `downloaded`). Sidebar cookies shrink each page ~50%.
3. **Parse** — reads `raw_posts`, extracts structured data (players, scores, maps, tournaments) into the normalized tables, marking each post `parsed` or `skipped` (with a reason).
4. **Rank** — computes **Elo** and **Glicko-2** ratings from the parsed matches.
5. **Serve** — exposes the results via a web app, a JSON API, and Discord/Twitch bots.

All components are supervised by an orchestrator (`engine.py`) that runs each stage as its own process in dependency order and restarts any that crash. Stages are event consumers: they only do work when the queue has items, so an up-to-date pipeline sleeps instead of polling. See [Pipeline architecture](#pipeline-architecture).

Data is stored in **ClickHouse**.

### Download modes

The download stage has two modes, selected by `DOWNLOAD_MODE` (default `discovery`):

- **`discovery`** (default) — a separate discovery stage scans the matchlist and registers match IDs in `raw_posts` (status `discovered`). The downloader then fetches only those match pages. This is efficient (only match pages, not every post) and gives a reliable chronological key (`sort_time` from the matchlist) — important because `post_id` is **not** chronological (a match post can be created long after it was played).
- **`sequential`** — scans `/post/1` → `/post/N` and stores every page. A robust fallback that catches everything (including matches the matchlist misses), but downloads far more (news, VODs, forum threads, deleted posts) and can only approximate chronology.

Both modes write into the same central `raw_posts` table; the parser orders processing by `sort_time` so ratings are computed chronologically in either mode.

## Pipeline architecture

The pipeline is an **event-worker pool** (Option A): each stage runs as an isolated process, supervised by `engine.py`. Stages claim work from a shared ClickHouse-backed queue (the `raw_posts.status` state machine) rather than polling on a fixed timer.

```
 PlusForward matchlist ──> discovery (match IDs) ─┐
 PlusForward posts /post/1..N ───────────────────┤ (sequential mode)
                                                  ▼
 ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
 │  download   │──>│    parse    │──>│    rank     │
 └─────────────┘   └─────────────┘   └─────────────┘
        │               │               │
        └───────────────┴───────┬───────┘
                                ▼
                         ┌─────────────┐
                         │  ClickHouse │  (queue + state + dead-letter)
                         └─────────────┘
                                │
          ┌─────────────────────┼─────────────────────┐
          ▼                     ▼                     ▼
     ┌─────────┐           ┌─────────┐           ┌─────────┐
     │   web   │           │ Discord │           │  Twitch │
     │(FastAPI)│           │   bot   │           │   bot   │
     └─────────┘           └─────────┘           └─────────┘
```

Key properties:
- **Event-gated** — `download`/`parse`/`rank` only process when the queue has items; a caught-up stage reports `idle` and sleeps cheaply. `discovery` polls the external site but paces itself (60s idle) and backs off on failure.
- **Crash-safe claiming** — work is claimed with a self-expiring lease (`raw_posts.locked_until`); a worker that dies mid-batch has its rows re-claimed after the lease expires.
- **Backoff + circuit breaker** — transient failures back off exponentially; a dead dependency (PlusForward, ClickHouse) trips a breaker instead of being hammered.
- **Dead-letter** — posts that exhaust their retries move to `failed_posts` (visible + manually retryable) instead of blocking the queue forever.
- **Real crash semantics** — a fatal stage error exits non-zero, so the supervisor + systemd `Restart=on-failure` actually restart it.
- **Observability** — every stage writes status (processed, queue depth, lag) to `pipeline_status`, surfaced on the dashboard.

### Components

- **Orchestrator** (`engine.py`) — supervises stage processes, restarts crashed ones, forwards signals.
- **Stage processes** (`python -m engine.stage <download|parse|rank|discovery>`) — the event consumers.
- **Engine internals** — `engine/queue.py` (lease claiming + dead-letter), `engine/runner.py` (stage event loop), `engine/status.py` (dashboard status writer), `src/backoff.py` (exponential backoff + circuit breaker).
- **External services** (`bot_discord.py`, `bot_twitch.py`, `api_web.py`) — long-lived socket processes, run as-is under the orchestrator.
- **Shared logic** lives in `src/`: post download, parsing, rankings computation, the database client/schema, the data provider, and the bots/web app.
- **`cli.py`** provides a command-line interface into the same data.

## Features

- **Two rating systems** — Elo (experience-aware K-factor × tournament tier multiplier) and Glicko-2 (with configurable rating period, tau, and volatility). Glicko-2 leaderboards/peaks use the conservative lower bound `rating − RD`.
- **Web app** (FastAPI + Jinja2): home page, leaderboards per game, player pages with rating-history charts, match pages, tournament pages, head-to-head (H2H) comparisons, and a JSON API at `/api/docs` (Swagger).
  - Day/night theme toggle, live table sorting/filtering, autocomplete search, smart match-mode detection for player filters.
- **Discord bot** — slash commands for rankings, player ratings, history, and H2H.
- **Twitch bot** — chat commands in one or multiple channels.
- **CLI** — `top`, `player`, `history`, `h2h`, `matches`, `player-matches`, `stats`, `games`, `tournaments`.
- **Daemon supervisor** — unified logging (shared config, stdout + optional rotating file); crash-restart for every component.

## Requirements

- **Python 3.10+**
- **ClickHouse** running locally (default `localhost:9000`, database `arena_rankings`)
- **pip** packages: `clickhouse-driver`, `fastapi`, `uvicorn`, `jinja2`, `python-dotenv`, `discord.py` (optional, for the Discord bot), `python-socketio`/`requests` (as used by the Twitch bot).

> A `requirements.txt` is not currently committed — install the imports used by the modules you run.

## Setup

```bash
# 1. Install Python dependencies
pip install clickhouse-driver fastapi uvicorn jinja2 python-dotenv discord.py

# 2. Configure environment
cp .env.example .env
#   edit .env — set ClickHouse credentials and bot tokens (see Configuration)

# 3. Initialize the database schema
python -c "from src.db_client import Database; Database().init_schema()"
```

### Configuration

All settings come from environment variables (loaded from `.env` via `python-dotenv`):

| Variable | Default | Description |
|---|---|---|
| `CLICKHOUSE_HOST` / `PORT` / `DATABASE` | `localhost` / `9000` / `arena_rankings` | ClickHouse connection |
| `CLICKHOUSE_USER` / `PASSWORD` | `default` / `quakepass` | ClickHouse credentials |
| `DISCORD_BOT_TOKEN` | — | Discord bot token (required for the Discord bot) |
| `TWITCH_BOT_TOKEN` / `TWITCH_CHANNEL` / `TWITCH_NICKNAME` | — / — / `arenabot` | Twitch bot token, channels (comma-separated), nickname |
| `RATE_LIMIT_DELAY` | `0.0` | Delay (s) between HTTP requests |
| `HTTP_TIMEOUT` | `3` | Request timeout (s) |
| `DOWNLOAD_MODE` | `discovery` | `discovery` (matchlist → match pages) or `sequential` (scan all posts) |
| `DOWNLOADER_WORKERS` | `1` | Concurrent download workers |
| `WALL_CONSECUTIVE` | `100` | Consecutive invalid posts before treating as the end (sequential mode) |
| `PARSER_WORKERS` | CPU count | Concurrent parser threads |
| `MIN_MATCHES_ELO` / `MIN_MATCHES_GLICKO2` | `0` / `30` | Minimum matches before a player appears |
| `GLICKO2_PERIOD` | `month` | Rating period: `year` / `month` / `week` / `day` |
| `GLICKO2_TAU` | `1.2` | Glicko-2 system constant (0.2 stable – 1.2 volatile) |
| `WEB_HOST` / `WEB_PORT` | `0.0.0.0` / `8080` | Web server bind address/port |

See `config.py` for the full list, including the Elo K-factor table and tournament tier multipliers.

## Usage

### Run the full pipeline (orchestrator)

```bash
python engine.py                 # run all stages + services (supervised)
# per-stage tuning is via systemd or the stage entrypoint, e.g.:
python -m engine.stage download --workers 3   # run just the download stage
python -m engine.stage discovery              # run just discovery
python -m engine.stage rank                   # run just rank
```

### Run components individually

```bash
python -m engine.stage discovery     # scan PlusForward matchlist (discovery mode)
python -m engine.stage download      # download match pages (event consumer)
python -m engine.stage parse         # parse HTML -> ClickHouse (event consumer)
python -m engine.stage rank          # compute ratings (event-gated, self-healing)
python api_web.py --port 8080        # web site + JSON API
python bot_discord.py                # Discord bot
python bot_twitch.py --channel chan1 # Twitch bot
```

In `sequential` mode (`DOWNLOAD_MODE=sequential`) there is no discovery stage — the downloader scans `/post/1..N` directly and discovery is not run.

Stage entrypoints accept `--workers N`, `--limit N`, `--idle SECONDS` (idle delay when no work), `--max-backoff N`, and `-v`.

### Pipeline dashboard

Run the web app and open `http://localhost:8080/pipeline` for a live view of every stage (status, processed count, queue depth, lag) plus the dead-letter quarantine with manual retry. Data comes from the `pipeline_status` and `failed_posts` tables.

### Logging

All components log through a single shared config (`src/logging_setup.py`), producing one uniform line format:

```
2026-08-16 23:15:11 INFO  [download] message
```

- Output always goes to **stdout** (systemd/journalctl under the service).
- Pass `--log-file PATH` (or set `LOG_FILE`/`LOG_DIR`) to also write to a **rotating file** (`logs/arena.log` by default, 10 MB × 5 backups).
- `-v` forces DEBUG; otherwise `LOG_LEVEL` applies (default `DEBUG`).
- Noisy third-party loggers (clickhouse_driver, discord.*, urllib3, asyncio, tzlocal) are silenced to WARNING automatically.

### CLI queries

```bash
python cli.py top --game "Quake Champions" --system glicko2 --limit 10
python cli.py player rapha
python cli.py history rapha --system elo
python cli.py h2h rapha "Agent 3K" --game "Quake Champions"
python cli.py matches --limit 20
python cli.py stats
python cli.py games
python cli.py tournaments
```

### Reset / reinitialize data

```bash
python reset.py rankings         # clear ratings + history (recomputed next cycle)
python reset.py parsed           # clear parsed data, reset status to 'discovered'
python reset.py all              # drop + recreate database (backs up downloaded data first)
python reset.py --dry-run all    # preview what would be reset
```

`reset.py` stops and restarts the pipeline automatically if it was running. The
pipeline is self-healing: the rank stage auto-detects empty/mismatched/corrupted
ratings and recomputes from scratch, so no manual `--reset` is needed.

### Backup / restore

```bash
python backup.py                          # full backup -> backups/<db>_<ts>.tar.gz
python backup.py --table matches          # backup a single table
python backup.py --restore FILE           # restore from a backup archive
```

Backups are a single Parquet+zstd archive (all tables, ~50x smaller than the
raw data). `reset.py all` uses the same backup/restore path to preserve the
downloaded data across a full reset.

### systemd

A `systemd` unit (`arena-rankings.service`) is included to run the pipeline as a service:

```bash
sudo cp arena-rankings.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now arena-rankings
```

## Web app

Run `python api_web.py` and open `http://localhost:8080`.

- **Home** — top players across games with a period filter.
- **Leaderboard** — per-game rankings, sortable, with inline filters (Elo / Glicko-2, tier, rating range).
- **Player pages** — all ratings and a rating-history chart (`/player/{id}/{name}`).
- **Matches / Tournament pages** — match details, map results, tournament metadata and rankings.
- **H2H** — head-to-head comparison between two players.
- **JSON API** — interactive docs at `/api/docs` (endpoints under `/api/...`).

## Project layout

```
arena-rankings/
├── cli.py                  # command-line interface
├── config.py               # all configuration (env-driven)
├── engine.py               # orchestrator: supervises stage processes
├── engine/
│   ├── queue.py            # lease-based claiming + dead-letter
│   ├── runner.py           # per-stage event loop
│   ├── status.py           # pipeline_status / failed_posts writer + reader
│   ├── stage.py            # per-stage process entrypoint
│   └── stages/
│       ├── discovery.py    # PlusForward matchlist scanner (polling, paced)
│       ├── download.py     # download event consumer
│       ├── parse.py        # parse event consumer
│       └── rank.py         # rank event consumer (watermark-gated)
├── src/
│   ├── backoff.py          # exponential backoff + circuit breaker
│   ├── db_client.py        # ClickHouse client
│   ├── db_schema.py        # DDL schema
│   ├── data_provider.py    # shared query layer (CLI/bots/web)
│   ├── match_discovery.py  # PlusForward matchlist scanning
│   ├── match_downloader.py # batch HTML downloader (discovery mode)
│   ├── post_downloader.py  # sequential /post/1..N downloader (sequential mode)
│   ├── match_parser.py     # HTML -> structured data
│   ├── tournament_resolver.py
│   ├── rankings_compute.py # Elo + Glicko-2 computation
│   ├── api_web.py          # FastAPI app + routes
│   ├── daemon.py           # legacy run_daemon (used by the bot/web wrappers)
│   ├── bot_discord.py      # Discord slash commands
│   ├── bot_twitch.py       # Twitch chat commands
│   ├── table.py            # ASCII/table formatting
│   ├── web_templates/      # Jinja2 templates
│   └── web_static/         # CSS, JS, chart library
├── api_web.py              # web server wrapper (--daemon)
├── bot_discord.py          # Discord bot wrapper (--daemon)
├── bot_twitch.py           # Twitch bot wrapper (--daemon)
├── reset.py                # database reset tool
├── backup.py               # backup/restore (Parquet+zstd single archive)
```

## License

Not specified. Reach out to the maintainer for licensing terms.

"""Arena Rankings — web site (FastAPI + Jinja2).

Serves HTML pages + a JSON API over the shared DataProvider layer.
Runs standalone (uvicorn) or as a daemon component via api_web.py wrapper.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .data_provider import DataProvider

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "web_templates"
STATIC_DIR = BASE_DIR / "web_static"

app = FastAPI(title="Arena Rankings", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Defaults (mirror config.py)
DEFAULT_LIMIT = 20
MIN_MATCHES_ELO = 0
MIN_MATCHES_GLICKO2 = 30

GAME_ALIASES = {
    "ql": "Quake Live",
    "qc": "Quake Champions",
    "q3": "Quake 3",
    "cpm": "CPMA",
    "q4": "Quake 4",
    "qw": "QuakeWorld",
}


def _resolve_game(game: str) -> str:
    """Resolve a game alias to its canonical name ("" = combined)."""
    if not game:
        return ""
    g = game.strip().lower()
    if g in GAME_ALIASES:
        return GAME_ALIASES[g]
    return game


def _min_matches(system: str) -> int:
    return MIN_MATCHES_GLICKO2 if system == "glicko2" else MIN_MATCHES_ELO


def _fmt_rating(r: float) -> str:
    return f"{r:,.0f}"


def _fmt_date(d) -> str:
    if d is None:
        return "—"
    s = str(d)
    return s[:10]


# ─── Context helpers ────────────────────────────────────────────────────────

def _base_context(request: Request, dx: DataProvider) -> dict:
    return {
        "request": request,
        "games": dx.get_games(),
        "game_aliases": GAME_ALIASES,
        "active": "",
    }


# ─── Pages ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with DataProvider() as dx:
        ctx = _base_context(request, dx)
        ctx["active"] = "home"
        ctx["stats"] = dx.get_stats()
        ctx["tier_stats"] = dx.get_tournament_stats()
        ctx["top_elo"] = dx.get_top_players(game="", system="elo", limit=10, min_matches=MIN_MATCHES_ELO)
        # Batch-fetch glicko2 ratings for the top Elo players (combined)
        glicko = dx.get_ratings_for_players([p["player_id"] for p in ctx["top_elo"]], system="glicko2", game="")
        for p in ctx["top_elo"]:
            p["glicko"] = glicko.get(p["player_id"])
        ctx["top_glicko"] = dx.get_top_players(game="", system="glicko2", limit=10, min_matches=MIN_MATCHES_GLICKO2)
    return templates.TemplateResponse(request, "home.html", ctx)


@app.get("/leaderboard", response_class=HTMLResponse)
def leaderboard(
    request: Request,
    game: str = Query("", description="Game name or alias (empty = combined)"),
    system: str = Query("elo", pattern="^(elo|glicko2)$"),
    sort: str = Query("rating", pattern="^(rating|peak)$"),
    limit: int = Query(50, ge=1, le=500),
    date: Optional[str] = Query(None, description="Leaderboard as of YYYY-MM-DD"),
):
    game = _resolve_game(game)
    with DataProvider() as dx:
        ctx = _base_context(request, dx)
        ctx["active"] = "leaderboard"
        ctx["game"] = game
        ctx["system"] = system
        ctx["sort"] = sort
        ctx["limit"] = limit
        ctx["date"] = date
        ctx["min_matches"] = _min_matches(system)
        if date:
            rows = dx.get_top_players_asof(
                date=date, game=game, system=system, limit=limit,
                min_matches=_min_matches(system), sort_by=sort,
            )
        else:
            rows = dx.get_top_players(
                game=game, system=system, limit=limit,
                min_matches=_min_matches(system), sort_by=sort,
            )
        ctx["players"] = rows
    return templates.TemplateResponse(request, "leaderboard.html", ctx)


@app.get("/player/{name}", response_class=HTMLResponse)
def player(
    request: Request,
    name: str,
    game: str = Query("", description="Game name or alias (empty = combined)"),
    limit: int = Query(50, ge=1, le=200),
    period: str = Query("all", description="History period: 1m, 3m, 6m, 1y, all"),
):
    game = _resolve_game(game)
    # Map period to a 'since' date (ISO) for the rating history
    since = ""
    period_map = {"1m": 30, "3m": 90, "6m": 180, "1y": 365}
    if period in period_map:
        since = (datetime.now() - timedelta(days=period_map[period])).strftime("%Y-%m-%d")
    with DataProvider() as dx:
        ctx = _base_context(request, dx)
        ctx["active"] = "player"
        ctx["game"] = game
        ctx["limit"] = limit
        ctx["period"] = period
        ctx["player_name"] = name
        ctx["ratings"] = dx.get_player_ratings(name)
        ctx["history"] = dx.get_player_history_both(name, game=game, limit=limit, since=since)
        # Convert datetimes to ISO strings for JSON serialization in the chart
        for h in ctx["history"]:
            if h.get("played_at") is not None:
                h["played_at"] = h["played_at"].strftime("%Y-%m-%d")
        ctx["matches"] = dx.get_player_matches(name, game=game, limit=20)
        ctx["rank_elo"] = dx.get_player_rank(name, game=game, system="elo", min_matches=MIN_MATCHES_ELO)
        ctx["rank_glicko"] = dx.get_player_rank(name, game=game, system="glicko2", min_matches=MIN_MATCHES_GLICKO2)
        # Resolve canonical name from first rating row if available
        if ctx["ratings"]:
            ctx["player_name"] = ctx["ratings"][0]["name"]
    return templates.TemplateResponse(request, "player.html", ctx)


@app.get("/matches", response_class=HTMLResponse)
def matches(
    request: Request,
    game: str = Query("", description="Game name or alias (empty = all)"),
    limit: int = Query(50, ge=1, le=200),
):
    game = _resolve_game(game)
    with DataProvider() as dx:
        ctx = _base_context(request, dx)
        ctx["active"] = "matches"
        ctx["game"] = game
        ctx["limit"] = limit
        ctx["matches"] = dx.get_recent_matches(game=game, limit=limit)
    return templates.TemplateResponse(request, "matches.html", ctx)


@app.get("/tournaments", response_class=HTMLResponse)
def tournaments(
    request: Request,
    tier: str = Query("", pattern="^(|premier|major|minor)$"),
    game: str = Query("", description="Game name or alias (empty = all)"),
    limit: int = Query(50, ge=1, le=200),
):
    game = _resolve_game(game)
    with DataProvider() as dx:
        ctx = _base_context(request, dx)
        ctx["active"] = "tournaments"
        ctx["tier"] = tier
        ctx["game"] = game
        ctx["limit"] = limit
        ctx["tournaments"] = dx.get_tournaments(tier=tier, limit=limit)
        ctx["tier_stats"] = dx.get_tournament_stats()
    return templates.TemplateResponse(request, "tournaments.html", ctx)


@app.get("/h2h", response_class=HTMLResponse)
def h2h(
    request: Request,
    p1: str = Query("", description="Player 1 name"),
    p2: str = Query("", description="Player 2 name"),
    game: str = Query("", description="Game name or alias (empty = all)"),
):
    game = _resolve_game(game)
    with DataProvider() as dx:
        ctx = _base_context(request, dx)
        ctx["active"] = "h2h"
        ctx["game"] = game
        ctx["p1"] = p1
        ctx["p2"] = p2
        ctx["result"] = None
        if p1 and p2:
            ctx["result"] = dx.get_head_to_head(p1, p2, game=game)
    return templates.TemplateResponse(request, "h2h.html", ctx)


# ─── JSON API ───────────────────────────────────────────────────────────────

@app.get("/api/stats")
def api_stats():
    with DataProvider() as dx:
        return dx.get_stats()


@app.get("/api/games")
def api_games():
    with DataProvider() as dx:
        return {"games": dx.get_games()}


@app.get("/api/leaderboard")
def api_leaderboard(
    game: str = "",
    system: str = "elo",
    sort: str = "rating",
    limit: int = 50,
    date: Optional[str] = None,
):
    game = _resolve_game(game)
    with DataProvider() as dx:
        if date:
            rows = dx.get_top_players_asof(date=date, game=game, system=system, limit=limit,
                                           min_matches=_min_matches(system), sort_by=sort)
        else:
            rows = dx.get_top_players(game=game, system=system, limit=limit,
                                      min_matches=_min_matches(system), sort_by=sort)
        return {"game": game, "system": system, "players": rows}


@app.get("/api/player/{name}")
def api_player(name: str, game: str = ""):
    game = _resolve_game(game)
    with DataProvider() as dx:
        return {
            "name": name,
            "ratings": dx.get_player_ratings(name),
            "history": dx.get_player_history_both(name, game=game),
            "matches": dx.get_player_matches(name, game=game, limit=20),
        }


@app.get("/api/matches")
def api_matches(game: str = "", limit: int = 50):
    game = _resolve_game(game)
    with DataProvider() as dx:
        return {"matches": dx.get_recent_matches(game=game, limit=limit)}


@app.get("/api/tournaments")
def api_tournaments(tier: str = "", game: str = "", limit: int = 50):
    game = _resolve_game(game)
    with DataProvider() as dx:
        return {
            "tournaments": dx.get_tournaments(tier=tier, limit=limit),
            "tier_stats": dx.get_tournament_stats(),
        }


@app.get("/api/h2h")
def api_h2h(p1: str, p2: str, game: str = ""):
    game = _resolve_game(game)
    with DataProvider() as dx:
        return dx.get_head_to_head(p1, p2, game=game)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("WEB_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)

"""Arena Rankings — web site (FastAPI + Jinja2).

Serves HTML pages + a JSON API over the shared DataProvider layer.
Runs standalone (uvicorn) or as a daemon component via api_web.py wrapper.
"""
from __future__ import annotations

import os
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .data_provider import DataProvider, _sort_matches
from config import MIN_MATCHES_ELO, MIN_MATCHES_GLICKO2

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "web_templates"
STATIC_DIR = BASE_DIR / "web_static"

app = FastAPI(title="Arena Rankings", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _qs(**kwargs) -> str:
    """Build a URL query string from non-empty kwargs (Jinja global `qs`)."""
    parts = []
    for k, v in kwargs.items():
        if v is None or v == "":
            continue
        parts.append(f"{k}={urllib.parse.quote(str(v))}")
    return "&".join(parts)


import re as _re

_REGEX_META = set("^$*+?|()[]\\{}")


def _smart_match_mode(q: str, is_exact_name) -> str:
    """Pick a match mode automatically from a filter query (no manual selector).

    - If the query contains regex metacharacters -> 'regex'
    - Else if it's an exact known name -> 'exact'
    - Else -> 'partial' (case-insensitive substring)
    """
    if not q:
        return "exact"
    if any(c in _REGEX_META for c in q):
        return "regex"
    if is_exact_name(q):
        return "exact"
    return "partial"


templates.env.globals["qs"] = _qs

# Defaults
DEFAULT_LIMIT = 20

GAME_ALIASES = {
    "ql": "Quake Live",
    "qc": "Quake Champions",
    "q3": "Quake 3 Arena",
    "cpm": "Quake 3 CPMA",
    "q4": "Quake 4",
    "qw": "Quake World",
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


def _best_rating(ratings: list[dict]) -> Optional[dict]:
    """Return the highest-current-rating entry for a player, or None."""
    if not ratings:
        return None
    return max(ratings, key=lambda r: r.get("rating") or 0)


def _ratings_by_system(ratings: list[dict]) -> dict:
    """Return {system: best-rating-entry} for a player (best across games per system)."""
    out: dict = {}
    for r in ratings or []:
        sys_name = r.get("system") or ""
        if not sys_name:
            continue
        cur = out.get(sys_name)
        if cur is None or (r.get("rating") or 0) > (cur.get("rating") or 0):
            out[sys_name] = r
    return out


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
def home(request: Request, sort: str = Query("elo", pattern="^(elo|glicko2)$")):
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
        # Batch-fetch elo ratings for the top Glicko-2 players (combined)
        elo = dx.get_ratings_for_players([p["player_id"] for p in ctx["top_glicko"]], system="elo", game="")
        for p in ctx["top_glicko"]:
            p["elo"] = elo.get(p["player_id"])
        ctx["top_sort"] = sort
        ctx["top_players"] = ctx["top_glicko"] if sort == "glicko2" else ctx["top_elo"]
        ctx["recent_matches"] = dx.get_recent_matches(limit=8)
        ctx["most_active"] = dx.get_most_active_players(limit=10)
        ctx["peak_info"] = dx.get_peak_rating_overall(system="elo", min_matches=MIN_MATCHES_ELO)
        ctx["peak_info_glicko"] = dx.get_peak_rating_overall(system="glicko2", min_matches=MIN_MATCHES_GLICKO2)
    return templates.TemplateResponse(request, "home.html", ctx)


@app.get("/top-players", response_class=HTMLResponse)
def top_players_partial(request: Request, sort: str = Query("elo", pattern="^(elo|glicko2)$")):
    """Return just the Top Players table HTML for in-place (AJAX) sorting."""
    with DataProvider() as dx:
        if sort == "glicko2":
            players = dx.get_top_players(game="", system="glicko2", limit=10, min_matches=MIN_MATCHES_GLICKO2)
            elo = dx.get_ratings_for_players([p["player_id"] for p in players], system="elo", game="")
            for p in players:
                p["elo"] = elo.get(p["player_id"])
        else:
            players = dx.get_top_players(game="", system="elo", limit=10, min_matches=MIN_MATCHES_ELO)
            glicko = dx.get_ratings_for_players([p["player_id"] for p in players], system="glicko2", game="")
            for p in players:
                p["glicko"] = glicko.get(p["player_id"])
    return templates.TemplateResponse(request, "_top_players.html", {"top_players": players, "top_sort": sort})


@app.get("/leaderboard", response_class=HTMLResponse)
def leaderboard(
    request: Request,
    game: str = Query("", description="Game name or alias (empty = combined)"),
    system: str = Query("elo", pattern="^(elo|glicko2)$"),
    sort: str = Query("rating", pattern="^(rating|peak)$"),
    date: Optional[str] = Query(None, description="Leaderboard as of YYYY-MM-DD"),
    limit: str = Query("100", pattern="^(100|1000|all)$", description="Rows to show"),
    sort_col: str = Query("", description="Column to sort by (server-side, all data)"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$", description="Sort direction"),
    page: int = Query(1, ge=1, description="Page number"),
    partial: int = Query(0, ge=0, le=1, description="Return only the results partial (AJAX)"),
):
    game = _resolve_game(game)
    # Map limit: 'all' -> large number, else int
    limit_n = 100000 if limit == "all" else int(limit)
    # Pagination: only when a finite limit is set; 'all' shows everything on one page.
    per_page = limit_n if limit != "all" else 100000
    offset = (page - 1) * per_page if limit != "all" else 0
    with DataProvider() as dx:
        ctx = _base_context(request, dx)
        ctx["active"] = "leaderboard"
        ctx["game"] = game
        ctx["system"] = system
        ctx["sort"] = sort
        ctx["date"] = date
        ctx["limit"] = limit
        ctx["sort_col"] = sort_col
        ctx["sort_dir"] = sort_dir
        ctx["min_matches"] = _min_matches(system)
        if date:
            rows = dx.get_top_players_asof(
                date=date, game=game, system=system, limit=per_page,
                min_matches=_min_matches(system), sort_by=sort,
                sort_col=sort_col, sort_dir=sort_dir, offset=offset,
            )
            total = dx.count_top_players_asof(
                date=date, game=game, system=system, min_matches=_min_matches(system)
            )
        else:
            rows = dx.get_top_players(
                game=game, system=system, limit=per_page,
                min_matches=_min_matches(system), sort_by=sort,
                sort_col=sort_col, sort_dir=sort_dir, offset=offset,
            )
            total = dx.count_top_players(
                game=game, system=system, min_matches=_min_matches(system)
            )
        ctx["players"] = rows
        ctx["page"] = page
        ctx["total"] = total
        ctx["total_pages"] = max(1, (total + per_page - 1) // per_page) if limit != "all" else 1
        ctx["pagination_qs"] = {
            "game": game, "system": system, "sort": sort, "date": date,
            "limit": limit, "sort_col": sort_col, "sort_dir": sort_dir,
            "player": "", "tournament": "", "tier": "",
        }
    if partial:
        return templates.TemplateResponse(request, "_leaderboard_results.html", ctx)
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
        # Games the player actually has ratings for (for the history game selector)
        ctx["player_games"] = sorted({r["game"] for r in ctx["ratings"] if r["game"] != "Combined"})
        # Compute per-game rank for each rating row (for the rank column)
        for r in ctx["ratings"]:
            # Combined ratings are stored with game_name = '' in the DB; map back for the rank query
            rank_game = "" if r["game"] == "Combined" else r["game"]
            rk = dx.get_player_rank(name, game=rank_game, system=r["system"], min_matches=_min_matches(r["system"]))
            r["rank"] = rk["rank"] if rk else None
            r["rank_total"] = rk["total"] if rk else None
        # For 'all' period, show the full history (no truncation).
        hist_limit = 100000 if period == "all" else limit
        ctx["history"] = dx.get_player_history_both(name, game=game, limit=hist_limit, since=since)
        # Convert datetimes to ISO strings for JSON serialization in the chart
        for h in ctx["history"]:
            if h.get("played_at") is not None:
                h["played_at"] = h["played_at"].strftime("%Y-%m-%d")
        ctx["matches"] = dx.get_player_matches(name, game=game, limit=10)
        ctx["rank_elo"] = dx.get_player_rank(name, game=game, system="elo", min_matches=MIN_MATCHES_ELO)
        ctx["rank_glicko"] = dx.get_player_rank(name, game=game, system="glicko2", min_matches=MIN_MATCHES_GLICKO2)
        ctx["summary"] = dx.get_player_summary(name)
        # Resolve canonical name from first rating row if available
        if ctx["ratings"]:
            ctx["player_name"] = ctx["ratings"][0]["name"]
        # Convert match datetimes for template use
        for m in ctx["matches"]:
            if m.get("played_at") is not None:
                m["played_at_str"] = m["played_at"].strftime("%Y-%m-%d") if hasattr(m["played_at"], 'strftime') else str(m["played_at"])[:10]
            else:
                m["played_at_str"] = "—"
    return templates.TemplateResponse(request, "player.html", ctx)


@app.get("/matches", response_class=HTMLResponse)
def matches(
    request: Request,
    game: str = Query("", description="Game name or alias (empty = all)"),
    player: str = Query("", description="Filter by player name"),
    tournament: str = Query("", description="Filter by tournament name"),
    limit: str = Query("100", pattern="^(100|1000|all)$", description="Rows to show"),
    clear: int = Query(0, ge=0, le=1),
    page: int = Query(1, ge=1, description="Page number"),
    partial: int = Query(0, ge=0, le=1, description="Return only the results partial (AJAX)"),
    sort_col: str = Query("", description="Column to sort by (server-side, all data)"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$", description="Sort direction"),
):
    # "Clear filters" button resets all filters
    if clear:
        game = player = tournament = ""
    game = _resolve_game(game)
    # Map limit: 'all' -> large number, else int
    limit_n = 100000 if limit == "all" else int(limit)
    per_page = limit_n if limit != "all" else 100000
    offset = (page - 1) * per_page if limit != "all" else 0
    with DataProvider() as dx:
        # Smart match-mode auto-detection (no manual selector).
        player_match = _smart_match_mode(player, dx.name_exists)
        tournament_match = _smart_match_mode(tournament, dx.name_exists)
        ctx = _base_context(request, dx)
        ctx["active"] = "matches"
        ctx["game"] = game
        ctx["player"] = player
        ctx["tournament"] = tournament
        ctx["limit"] = limit
        ctx["sort_col"] = sort_col
        ctx["sort_dir"] = sort_dir
        ctx["matches"] = dx.get_recent_matches(game=game, limit=per_page, player=player, tournament=tournament, offset=offset, sort_col=sort_col, sort_dir=sort_dir, player_match=player_match, tournament_match=tournament_match)
        total = dx.count_recent_matches(game=game, player=player, tournament=tournament, player_match=player_match, tournament_match=tournament_match)
        ctx["page"] = page
        ctx["total"] = total
        ctx["total_pages"] = max(1, (total + per_page - 1) // per_page) if limit != "all" else 1
        ctx["pagination_qs"] = {
            "game": game, "player": player, "tournament": tournament, "limit": limit,
            "system": "", "sort": "", "date": "", "sort_col": sort_col, "sort_dir": sort_dir, "tier": "",
        }
        # Convert match datetimes for template use
        for m in ctx["matches"]:
            if m.get("played_at") is not None:
                m["played_at_str"] = m["played_at"].strftime("%Y-%m-%d") if hasattr(m["played_at"], 'strftime') else str(m["played_at"])[:10]
            else:
                m["played_at_str"] = "—"
    if partial:
        return templates.TemplateResponse(request, "_matches_results.html", ctx)
    return templates.TemplateResponse(request, "matches.html", ctx)


@app.get("/autocomplete", response_class=JSONResponse)
def autocomplete(
    request: Request,
    type: str = Query("player", pattern="^(player|tournament)$"),
    q: str = Query("", description="Partial query to match"),
    limit: int = Query(20, ge=1, le=50),
):
    """Return matching names for autocomplete dropdowns (AJAX).

    type is 'player' or 'tournament'. Matching is case-insensitive substring.
    Returns a JSON list of distinct names.
    """
    with DataProvider() as dx:
        names = dx.autocomplete(type, q, limit=limit)
    return {"names": names}


@app.get("/tournaments", response_class=HTMLResponse)
def tournaments(
    request: Request,
    tier: str = Query("", pattern="^(|premier|major|minor)$"),
    game: str = Query("", description="Game name or alias (empty = all)"),
    limit: str = Query("100", pattern="^(100|1000|all)$", description="Rows to show"),
    clear: int = Query(0, ge=0, le=1),
    page: int = Query(1, ge=1, description="Page number"),
    partial: int = Query(0, ge=0, le=1, description="Return only the results partial (AJAX)"),
    sort_col: str = Query("", description="Column to sort by (server-side, all data)"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$", description="Sort direction"),
):
    # "Clear filters" button resets all filters
    if clear:
        tier = game = ""
    game = _resolve_game(game)
    # Map limit: 'all' -> large number, else int
    limit_n = 100000 if limit == "all" else int(limit)
    per_page = limit_n if limit != "all" else 100000
    offset = (page - 1) * per_page if limit != "all" else 0
    with DataProvider() as dx:
        ctx = _base_context(request, dx)
        ctx["active"] = "tournaments"
        ctx["tier"] = tier
        ctx["game"] = game
        ctx["limit"] = limit
        ctx["sort_col"] = sort_col
        ctx["sort_dir"] = sort_dir
        ctx["tournaments"] = dx.get_tournaments(tier=tier, game=game, limit=per_page, offset=offset, sort_col=sort_col, sort_dir=sort_dir)
        total = dx.count_tournaments(tier=tier, game=game)
        ctx["page"] = page
        ctx["total"] = total
        ctx["total_pages"] = max(1, (total + per_page - 1) // per_page) if limit != "all" else 1
        ctx["pagination_qs"] = {"tier": tier, "game": game, "limit": limit,
                              "system": "", "sort": "", "date": "", "sort_col": sort_col,
                              "sort_dir": sort_dir, "player": "", "tournament": ""}
        ctx["tier_stats"] = dx.get_tournament_stats()
    if partial:
        return templates.TemplateResponse(request, "_tournaments_results.html", ctx)
    return templates.TemplateResponse(request, "tournaments.html", ctx)


@app.get("/h2h", response_class=HTMLResponse)
def h2h(
    request: Request,
    p1: str = Query("", description="Player 1 name"),
    p2: str = Query("", description="Player 2 name"),
    game: str = Query("", description="Game name or alias (empty = all)"),
    partial: int = Query(0, ge=0, le=1, description="Return only the results partial (AJAX)"),
    sort_col: str = Query("date", description="Column to sort match history by"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$", description="Sort direction"),
    clear: int = Query(0, ge=0, le=1),
):
    # "Clear filters" button resets all filters
    if clear:
        p1 = p2 = game = ""
    game = _resolve_game(game)
    with DataProvider() as dx:
        ctx = _base_context(request, dx)
        ctx["active"] = "h2h"
        ctx["game"] = game
        ctx["p1"] = p1
        ctx["p2"] = p2
        ctx["sort_col"] = sort_col
        ctx["sort_dir"] = sort_dir
        ctx["result"] = None
        if p1 and p2:
            # Smart match-mode auto-detection per player (no manual selector).
            p1_match = _smart_match_mode(p1, dx.name_exists)
            p2_match = _smart_match_mode(p2, dx.name_exists)
            ctx["result"] = dx.get_head_to_head(p1, p2, game=game, match=p1_match, match2=p2_match)
            # Server-side sort of the match history (consistent with matches/tournaments).
            ctx["result"]["matches"] = _sort_matches(ctx["result"]["matches"], sort_col, sort_dir)
            # Convert match datetimes for template use
            for m in ctx["result"]["matches"]:
                if m.get("played_at") is not None:
                    m["played_at_str"] = m["played_at"].strftime("%Y-%m-%d") if hasattr(m["played_at"], 'strftime') else str(m["played_at"])[:10]
                else:
                    m["played_at_str"] = "—"
            # Current rating for each player (best rating across systems/games)
            ctx["p1_ratings"] = _ratings_by_system(dx.get_player_ratings(p1))
            ctx["p2_ratings"] = _ratings_by_system(dx.get_player_ratings(p2))
    if partial:
        return templates.TemplateResponse(request, "_h2h_results.html", ctx)
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
        p1_match = _smart_match_mode(p1, dx.name_exists)
        p2_match = _smart_match_mode(p2, dx.name_exists)
        return dx.get_head_to_head(p1, p2, game=game, match=p1_match, match2=p2_match)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("WEB_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
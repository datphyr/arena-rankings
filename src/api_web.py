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
from markupsafe import Markup

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


def _slug(name: str) -> str:
    """URL-safe readability slug for a player name (lowercase, spaces->dashes).
    Used only for display in the URL; resolution is by player_id."""
    import re
    s = re.sub(r"[^\w\- ]", "", name or "").strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    return s or "player"


templates.env.globals["slug"] = _slug


def _match_slug(p1: str, p2: str) -> str:
    """Readability slug for a match URL: '{p1}-vs-{p2}' (lowercase, dashes).
    Used only for display; resolution is by match_id."""
    return f"{_slug(p1)}-vs-{_slug(p2)}"


templates.env.globals["match_slug"] = _match_slug


def _mapname(name: str) -> str:
    """Display name for a map. Unknown maps are stored as '?' — show 'unknown'."""
    name = (name or "").strip()
    return "Unknown" if name == "?" else name


templates.env.filters["mapname"] = _mapname


def _ranknum(pos) -> int:
    """Extract the numeric rank from an ordinal position string ('1st'->1,
    '2nd'->2, '3rd'->3, '10th'->10). Returns the int or 0 if not parseable."""
    s = str(pos or "").strip().lstrip("#")
    digits = ""
    for ch in s:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


templates.env.filters["ranknum"] = _ranknum


def _fmt_dt(d) -> str:
    """Format a datetime as 'YYYY-MM-DD, HH:MM' (comma between date and time).
    Shared by all templates via the 'dt' Jinja filter."""
    if d is None:
        return "—"
    if hasattr(d, "strftime"):
        return d.strftime("%Y-%m-%d, %H:%M")
    s = str(d)
    # Accept ISO-ish strings like '2018-12-02T13:00:00' or '2018-12-02 13:00:00'
    s = s.replace("T", " ")
    if len(s) >= 16:
        return s[:10] + ", " + s[11:16]
    return s[:10]


templates.env.filters["dt"] = _fmt_dt

# Deterministic per-player avatar gradient: hash the player_id to pick colors
# from the site palette, so every player gets a unique badge.
_AVATAR_PALETTE = [
    "#ef4444", "#f97316", "#f59e0b", "#eab308", "#84cc16", "#22c55e",
    "#10b981", "#14b8a6", "#06b6d4", "#0ea5e9", "#3b82f6", "#6366f1",
    "#8b5cf6", "#a855f7", "#d946ef", "#ec4899", "#f43f5e", "#fb7185",
    "#fda4af", "#fbbf24", "#fde68a", "#a3e635", "#4ade80", "#2dd4bf",
    "#22d3ee", "#38bdf8", "#60a5fa", "#818cf8", "#a78bfa", "#c084fc",
    "#e879f9", "#f472b6", "#f87171", "#fb923c", "#facc15", "#bef264",
    "#86efac", "#5eead4", "#67e8f9", "#93c5fd",
]


def _splitmix64(x: int) -> int:
    """splitmix64 finalizer — strong deterministic 64-bit mixing."""
    x = (x + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    return x ^ (x >> 31)


def _avatar_gradient(player_id) -> str:
    """Return a deterministic 4-corner CSS gradient for a player's avatar badge.
    Picks 4 distinct colors from the palette via a splitmix64 hash of the
    player_id, so every player gets a unique badge that's stable across visits.
    (40 colors → C(40,4)=91390 combos; top players are all unique, ~97% of all
    5447 players unique.)"""
    try:
        pid = int(player_id)
    except (TypeError, ValueError):
        pid = 0
    n = len(_AVATAR_PALETTE)
    s = _splitmix64(pid)
    pool = list(range(n))
    idx = []
    for _ in range(4):
        s = _splitmix64(s)
        j = s % len(pool)
        idx.append(pool.pop(j))
    c = [_AVATAR_PALETTE[i] for i in idx]
    # Conic gradient rotated 45° so the sweep starts at the top-left corner
    # (each corner gets its own color, radiating outward from the middle).
    return f"conic-gradient(from 45deg, {c[0]}, {c[1]}, {c[2]}, {c[3]}, {c[0]})"


templates.env.filters["avatar_gradient"] = _avatar_gradient

# ISO 3166-1 alpha-2 country code -> flag IMAGE (flagcdn.com).
# Emoji regional-indicator flags (U+1F1E6..) render as bare letters on many
# systems/fonts, so we use real flag images instead. Non-country codes
# (e.g. 'eu' = Europe, 'xx' = unknown) map to a neutral placeholder so the
# layout stays aligned; unknown/empty -> '' (no flag).
_FLAG_NEUTRAL = "🌐"
_FLAG_NON_COUNTRY = {"eu", "xx"}


def _flag(code: str) -> str:
    """Return an <img> flag (flagcdn.com) for an ISO 3166-1 alpha-2 code.

    Rendered as safe HTML (Markup) so the <img> isn't escaped. Non-country
    codes fall back to a neutral emoji placeholder.
    """
    if not code:
        return ""
    code = code.strip().lower()
    if len(code) != 2 or not code.isalpha():
        return ""
    if code in _FLAG_NON_COUNTRY:
        return _FLAG_NEUTRAL
    # Real flag image from flagcdn (SVG scales cleanly, no width variance).
    return Markup(
        f'<img class="flag-img" src="https://flagcdn.com/{code}.svg" '
        f'alt="{code.upper()}" title="{code.upper()}" loading="lazy">'
    )


templates.env.filters["flag"] = _flag

# Map a game name to its plusforward.net category-icon class (pf_categories font).
# Keys are the canonical game names used across the app. Unknown games render
# nothing (no icon) rather than a broken glyph.
_GAME_ICON_CLASS = {
    "Blood Run": "pfcat-br",
    "Diabotical": "pfcat-db",
    "Quake 2": "pfcat-q2",
    "Quake 3 Arena": "pfcat-q3a",
    "Quake 3 CPMA": "pfcat-cpma",
    "Quake 4": "pfcat-q4",
    "Quake Champions": "pfcat-qc",
    "Quake Live": "pfcat-ql",
    "Quake World": "pfcat-q1",
    "Reflex": "pfcat-rflx",
    "Unreal Tournament": "pfcat-ut",
    "Xonotic": "pfcat-xon",
}


def _game_icon(game: str) -> Markup:
    """Return a <i> game-icon glyph for a game name, or empty if unknown.

    Rendered as safe HTML (Markup). The glyph color follows the theme via CSS
    (white in dark mode, black in light mode).
    """
    if not game:
        return Markup("")
    cls = _GAME_ICON_CLASS.get(game.strip())
    if not cls:
        return Markup("")
    return Markup(f'<i class="game-icon {cls}" title="{game}"></i>')


templates.env.filters["game_icon"] = _game_icon

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
    """Resolve a game alias to its canonical name ("" = all games)."""
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
        ctx["top_elo"] = dx.get_top_players(game="", system="elo", limit=10, min_matches=MIN_MATCHES_ELO, fetch_peaks=False)
        # Batch-fetch glicko2 ratings for the top Elo players (combined)
        glicko = dx.get_ratings_for_players([p["player_id"] for p in ctx["top_elo"]], system="glicko2", game="")
        for p in ctx["top_elo"]:
            p["glicko"] = glicko.get(p["player_id"])
        ctx["top_glicko"] = dx.get_top_players(game="", system="glicko2", limit=10, min_matches=MIN_MATCHES_GLICKO2, fetch_peaks=False)
        # Batch-fetch elo ratings for the top Glicko-2 players (combined)
        elo = dx.get_ratings_for_players([p["player_id"] for p in ctx["top_glicko"]], system="elo", game="")
        for p in ctx["top_glicko"]:
            p["elo"] = elo.get(p["player_id"])
        ctx["top_sort"] = sort
        ctx["top_players"] = ctx["top_glicko"] if sort == "glicko2" else ctx["top_elo"]
        ctx["recent_matches"] = dx.get_recent_matches(limit=8)
        ctx["most_active"] = dx.get_most_active_players(limit=10)
        # Both overall peaks in a single rating_history scan.
        _peaks = dx.get_peak_rating_overall_both(
            min_matches={"elo": MIN_MATCHES_ELO, "glicko2": MIN_MATCHES_GLICKO2}
        )
        ctx["peak_info"] = _peaks.get("elo")
        ctx["peak_info_glicko"] = _peaks.get("glicko2")
    return templates.TemplateResponse(request, "home.html", ctx)


@app.get("/top-players", response_class=HTMLResponse)
def top_players_partial(request: Request, sort: str = Query("elo", pattern="^(elo|glicko2)$")):
    """Return just the Top Players table HTML for in-place (AJAX) sorting."""
    with DataProvider() as dx:
        if sort == "glicko2":
            players = dx.get_top_players(game="", system="glicko2", limit=10, min_matches=MIN_MATCHES_GLICKO2, fetch_peaks=False)
            elo = dx.get_ratings_for_players([p["player_id"] for p in players], system="elo", game="")
            for p in players:
                p["elo"] = elo.get(p["player_id"])
        else:
            players = dx.get_top_players(game="", system="elo", limit=10, min_matches=MIN_MATCHES_ELO, fetch_peaks=False)
            glicko = dx.get_ratings_for_players([p["player_id"] for p in players], system="glicko2", game="")
            for p in players:
                p["glicko"] = glicko.get(p["player_id"])
    return templates.TemplateResponse(request, "_top_players.html", {"top_players": players, "top_sort": sort})


@app.get("/leaderboard", response_class=HTMLResponse)
def leaderboard(
    request: Request,
    game: str = Query("", description="Game name or alias (empty = all games)"),
    system: str = Query("elo", pattern="^(elo|glicko2)$"),
    sort: str = Query("rating", pattern="^(rating|peak)$"),
    date: Optional[str] = Query(None, description="Leaderboard as of YYYY-MM-DD"),
    limit: str = Query("100", pattern="^(100|1000|all)$", description="Rows to show"),
    sort_col: str = Query("", description="Column to sort by (server-side, all data)"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$", description="Sort direction"),
    page: int = Query(1, ge=1, description="Page number"),
    partial: int = Query(0, ge=0, le=1, description="Return only the results partial (AJAX)"),
    clear: int = Query(0, ge=0, le=1),
):
    # "Clear filters" button resets all filters
    if clear:
        game = ""
        system = "elo"
        sort = "rating"
        date = None
        limit = "100"
        sort_col = ""
        sort_dir = "desc"
        page = 1
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
                fetch_peaks=(sort == "peak"),
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


@app.get("/player/{player_id}/{name}", response_class=HTMLResponse)
def player_by_id(
    request: Request,
    player_id: int,
    name: str,
    game: str = Query("", description="Game name or alias (empty = all games)"),
    limit: int = Query(50, ge=1, le=200),
    period: str = Query("all", description="History period: 1m, 3m, 6m, 1y, all"),
):
    # Two-segment path = id-based. The name segment is a readability slug only
    # — resolution is by id, so it's always unambiguous even if the slug is
    # stale or wrong. (Single-segment /player/{name} stays name-based.)
    with DataProvider() as dx:
        canonical = dx._canonical_name(player_id)
    return _player_page(request, canonical, game=game, limit=limit, period=period, player_id=player_id)


@app.get("/player/{player_id}", response_class=HTMLResponse)
def player_by_id_short(
    request: Request,
    player_id: int,
    game: str = Query("", description="Game name or alias (empty = all games)"),
    limit: int = Query(50, ge=1, le=200),
    period: str = Query("all", description="History period: 1m, 3m, 6m, 1y, all"),
):
    # Single numeric segment = id lookup (same page as /player/{id}/{slug}).
    # Note: a player whose canonical name is purely numeric (e.g. "2", id 1077)
    # is still reachable via its two-segment id URL /player/1077/2.
    with DataProvider() as dx:
        name = dx._canonical_name(player_id)
    return _player_page(request, name, game=game, limit=limit, period=period, player_id=player_id)


def _player_page(request: Request, name: str, game: str = "", limit: int = 50, period: str = "all", player_id: int | None = None):
    game = _resolve_game(game)
    # Rating history is always all-time (no period filter on the page).
    since = ""
    with DataProvider() as dx:
        ctx = _base_context(request, dx)
        ctx["active"] = "player"
        ctx["game"] = game
        ctx["limit"] = limit
        ctx["period"] = "all"
        ctx["player_name"] = name
        # Use the explicit id when known (id-based URL) so a name collision
        # between two players (e.g. two "serious") doesn't resolve to the wrong
        # player. Falls back to name resolution for name-based URLs.
        ctx["player_id"] = player_id if player_id is not None else dx._player_id(name)
        ctx["ratings"] = dx.get_player_ratings(name, min_matches={"glicko2": MIN_MATCHES_GLICKO2, "elo": MIN_MATCHES_ELO}, player_id=ctx["player_id"])
        # Games the player actually has ratings for (for the history game selector)
        ctx["player_games"] = sorted({r["game"] for r in ctx["ratings"] if r["game"] != "All Games"})
        # Compute per-game rank for each rating row (for the rank column) in a
        # single batched query instead of one get_player_rank call per row.
        if ctx["ratings"]:
            rank_combos = [
                {"game": "" if r["game"] == "All Games" else r["game"],
                 "system": r["system"],
                 "min_matches": _min_matches(r["system"])}
                for r in ctx["ratings"]
            ]
            ranks = dx.get_player_ranks(ctx["player_id"], rank_combos, player_ratings=ctx["ratings"])
            for r in ctx["ratings"]:
                rk = ranks.get(("" if r["game"] == "All Games" else r["game"], r["system"]))
                r["rank"] = rk["rank"] if rk else None
                r["rank_total"] = rk["total"] if rk else None
        # Rating history is always all-time (no truncation).
        hist_limit = 100000
        ctx["history"] = dx.get_player_history_both(name, game=game, limit=hist_limit, since=since, player_id=ctx["player_id"])
        # Convert datetimes to ISO strings for JSON serialization in the chart
        # (keep the full timestamp so same-day matches spread by time-of-day)
        for h in ctx["history"]:
            if h.get("played_at") is not None:
                h["played_at"] = h["played_at"].isoformat()
        # Fetch recent matches once (limit 100) and reuse for both the matches
        # list (sliced to 10) and the summary streak scan — avoids a redundant
        # get_player_matches(100) re-query inside get_player_summary.
        recent_matches = dx.get_player_matches(name, game=game, limit=100, player_id=ctx["player_id"])
        ctx["matches"] = recent_matches[:10]
        # Current-game rank for both systems — already computed in the batched
        # ranks call above (the current game is in ctx["ratings"]), so reuse it
        # instead of issuing a second get_player_ranks query.
        ctx["rank_elo"] = ranks.get((game, "elo"))
        ctx["rank_glicko"] = ranks.get((game, "glicko2"))
        ctx["summary"] = dx.get_player_summary(name, ratings=ctx["ratings"], recent_matches=recent_matches, player_id=ctx["player_id"])
        # Resolve canonical name from first rating row if available
        if ctx["ratings"]:
            ctx["player_name"] = ctx["ratings"][0]["name"]
        # Convert match datetimes for template use
        for m in ctx["matches"]:
            m["played_at_str"] = _fmt_dt(m.get("played_at"))
    return templates.TemplateResponse(request, "player.html", ctx)


@app.get("/match/{match_id}/{p1_slug}-vs-{p2_slug}/{tournament_slug}/{stage_slug}", response_class=HTMLResponse)
def match_details_slug(
    request: Request,
    match_id: int,
    p1_slug: str,
    p2_slug: str,
    tournament_slug: str,
    stage_slug: str,
):
    # Slug segments are readability only — resolution is by match_id, so the
    # URL works even if the slug is stale/wrong. Same page as /match/{id}.
    return _match_page(request, match_id)


@app.get("/match/{match_id}", response_class=HTMLResponse)
def match_details(request: Request, match_id: int):
    """Single match details page: header scoreboard + per-map breakdown."""
    return _match_page(request, match_id)


def _match_page(request: Request, match_id: int):
    with DataProvider() as dx:
        ctx = _base_context(request, dx)
        ctx["active"] = "matches"
        m = dx.get_match_details(match_id)
        if m is None:
            return templates.TemplateResponse(request, "match.html", ctx, status_code=404)
        ctx["match"] = m
        ctx["played_at_str"] = _fmt_dt(m.get("played_at"))
        # Ratings each player had just before the match (per game + system).
        ctx["pre_ratings"] = dx.get_ratings_before_match(match_id)
        # Per-match Elo + Glicko-2 deltas for both players (same game as the
        # match), for the delta display on the scoreboard.
        md = dx.get_matches_rating_deltas([match_id], game=m.get("game") or "")
        ctx["match_deltas"] = md.get(match_id, {})
        # Per-map winner names are already resolved; nothing extra needed.
    return templates.TemplateResponse(request, "match.html", ctx)


@app.get("/matches", response_class=HTMLResponse)
def matches(
    request: Request,
    game: str = Query("", description="Game name or alias (empty = all)"),
    player: str = Query("", description="Filter by player name"),
    player_id: int | None = Query(None, description="Filter by player id (from autocomplete; takes precedence over player)"),
    tournament: str = Query("", description="Filter by tournament name"),
    tier: str = Query("", pattern="^(|premier|major|minor)$", description="Filter by tournament tier"),
    limit: str = Query("100", pattern="^(100|1000|all)$", description="Rows to show"),
    clear: int = Query(0, ge=0, le=1),
    page: int = Query(1, ge=1, description="Page number"),
    partial: int = Query(0, ge=0, le=1, description="Return only the results partial (AJAX)"),
    sort_col: str = Query("", description="Column to sort by (server-side, all data)"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$", description="Sort direction"),
):
    # "Clear filters" button resets all filters
    if clear:
        game = player = tournament = tier = ""
        player_id = None
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
        ctx["player_id"] = player_id
        ctx["tournament"] = tournament
        ctx["tier"] = tier
        ctx["limit"] = limit
        ctx["sort_col"] = sort_col
        ctx["sort_dir"] = sort_dir
        ctx["matches"] = dx.get_recent_matches(game=game, limit=per_page, player=player, tournament=tournament, tier=tier, offset=offset, sort_col=sort_col, sort_dir=sort_dir, player_match=player_match, tournament_match=tournament_match, player_id=player_id)
        total = dx.count_recent_matches(game=game, player=player, tournament=tournament, tier=tier, player_match=player_match, tournament_match=tournament_match, player_id=player_id)
        # Stable column widths computed from the FULL (unfiltered) dataset so
        # fixed-layout columns don't shift when sorting/paginating/filtering.
        _cw = dx.matches_col_widths()
        _CH = 7.8  # approx monospace char width (px) at 0.92rem JetBrains Mono (measured 7.75)
        _PAD = 16  # cell padding + breathing room
        # Cap the flexible/truncatable columns (stage, tournament) so the
        # table fits a ~1280px viewport while player names keep full width.
        # Player names (player1/player2) are NOT capped — they must never crop.
        # Caps chosen so total fits 1280px container (~960px):
        #   date149 + p1(≤195) + score71 + p2(≤172) + stage100 + tourn220
        _MAX_STAGE = 100      # 'Grand Final Match #1' rare, truncation-OK
        _MAX_TOURNAMENT = 220 # game icon + tier pill prefix + name, truncation-OK
        ctx["col_widths"] = {
            "date": 17 * _CH + _PAD,          # 'YYYY-MM-DD, HH:MM' = 17 chars
            "player1": _cw.get("player1", 0) * _CH + _PAD,
            "player2": _cw.get("player2", 0) * _CH + _PAD,
            "score": 7 * _CH + _PAD,          # '999 : 999' worst case
            "stage": min(_cw.get("stage", 0) * _CH + _PAD, _MAX_STAGE),
            "tournament": min(_cw.get("tournament", 0) * _CH + _PAD, _MAX_TOURNAMENT),
        }
        ctx["page"] = page
        ctx["total"] = total
        ctx["total_pages"] = max(1, (total + per_page - 1) // per_page) if limit != "all" else 1
        ctx["pagination_qs"] = {
            "game": game, "player": player, "tournament": tournament, "tier": tier, "limit": limit,
            "system": "", "sort": "", "date": "", "sort_col": sort_col, "sort_dir": sort_dir,
        }
        # Convert match datetimes for template use
        for m in ctx["matches"]:
            m["played_at_str"] = _fmt_dt(m.get("played_at"))
    if partial:
        return templates.TemplateResponse(request, "_matches_results.html", ctx)
    return templates.TemplateResponse(request, "matches.html", ctx)


@app.get("/rivals", response_class=HTMLResponse)
def rivals(
    request: Request,
    player: str = Query("", description="Filter by player name"),
    player_id: int | None = Query(None, description="Filter by player id (from autocomplete; takes precedence over player)"),
    game: str = Query("", description="Filter by game ('' = all games)"),
    partial: int = Query(0, ge=0, le=1, description="Return only the results partial (AJAX)"),
    clear: int = Query(0, ge=0, le=1),
):
    # "Clear filters" button resets all filters
    if clear:
        player = ""
        player_id = None
        game = ""
    with DataProvider() as dx:
        ctx = _base_context(request, dx)
        ctx["active"] = "rivals"
        ctx["player"] = player
        ctx["player_id"] = player_id
        ctx["game"] = _resolve_game(game)
        ctx["games"] = dx.get_games()
        # Resolve the player for the header/name display (id-authoritative).
        resolved_id = player_id if player_id is not None else dx._player_id(player)
        ctx["rivals_player_id"] = resolved_id
        ctx["rivals_player_name"] = dx._canonical_name(resolved_id) if resolved_id else player
        ctx["total"] = 0
        ctx["rivals"] = []
        if resolved_id:
            ctx["rivals"] = dx.get_player_rivals(ctx["rivals_player_name"], player_id=resolved_id, limit=100000, game=ctx["game"])
            ctx["total"] = len(ctx["rivals"])
    if partial:
        return templates.TemplateResponse(request, "_rivals_results.html", ctx)
    return templates.TemplateResponse(request, "rivals.html", ctx)


@app.get("/autocomplete", response_class=JSONResponse)
def autocomplete(
    request: Request,
    type: str = Query("player", pattern="^(player|tournament)$"),
    q: str = Query("", description="Partial query to match"),
    limit: int = Query(20, ge=1, le=50),
):
    """Return matching names for autocomplete dropdowns (AJAX).

    type is 'player' or 'tournament'. Matching is case-insensitive substring.
    For players, returns a JSON list of {"name", "id"} dicts (one per
    player_id, so case-distinct players like 'pavel'/'Pavel' both appear).
    For tournaments, returns a JSON list of plain name strings.
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
    # Default sort: last match descending (matches the SQL ORDER BY default).
    if not sort_col:
        sort_col = "last_match"
        sort_dir = "desc"
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
        # Stable column widths computed from the FULL (unfiltered) dataset so
        # fixed-layout columns don't shift when sorting/filtering/paginating.
        _cw = dx.tournaments_col_widths()
        _CH = 7.5  # approx monospace char width (px) at 0.92rem JetBrains Mono
        _PAD = 16  # cell padding + breathing room
        # Cap the tournament name column so a single very long name doesn't
        # blow out the layout and push the last column off-screen.
        _MAX_NAME = 300
        ctx["col_widths"] = {
            "name": min(_cw.get("name", 0) * _CH + _PAD, _MAX_NAME),
            "matches": max(_cw.get("matches", 0), len("Matches")) * _CH + _PAD,
            "players": max(_cw.get("players", 0), len("Players")) * _CH + _PAD,
            "maps": max(_cw.get("maps", 0), len("Maps")) * _CH + _PAD,
            "date": max(10, len("Last Match")) * _CH + _PAD + 24,  # header 'LAST MATCH' + sort arrow space
        }
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


@app.get("/tournament/{tournament_id}/{name}", response_class=HTMLResponse)
def tournament_details(
    request: Request,
    tournament_id: int,
    name: str,
):
    # Two-segment path = id-based. The name segment is a readability slug only
    # — resolution is by id, so it's always unambiguous even if the slug is
    # stale or wrong (or the name collides with another tournament).
    return _tournament_page(request, tournament_id)


@app.get("/tournament/{tournament_id}", response_class=HTMLResponse)
def tournament_details_short(request: Request, tournament_id: int):
    # Single numeric segment = id lookup (same page as /tournament/{id}/{slug}).
    return _tournament_page(request, tournament_id)


def _tournament_page(request: Request, tournament_id: int):
    import json as _json
    with DataProvider() as dx:
        ctx = _base_context(request, dx)
        ctx["active"] = "tournaments"
        det = dx.get_tournament_details(tournament_id)
        if det is None:
            return templates.TemplateResponse(request, "tournament.html", ctx, status_code=404)
        ctx["t"] = det
        # Parse rankings JSON for the template.
        try:
            ctx["rankings"] = _json.loads(det.get("rankings") or "[]")
        except Exception:
            ctx["rankings"] = []
        # Enrich each standings row with the player's country (for the flag).
        if ctx["rankings"]:
            rids = [r.get("player_id") for r in ctx["rankings"] if r.get("player_id")]
            countries = dx._countries(list(set(rids))) if rids else {}
            for r in ctx["rankings"]:
                r["country"] = countries.get(r.get("player_id"), "")
        # Enrich each standings row with Elo + Glicko-2 rating deltas over the
        # tournament's own time window (for the colored delta columns).
        if ctx["rankings"]:
            deltas = dx.get_tournament_rating_deltas(tournament_id)
            for r in ctx["rankings"]:
                d = deltas.get(r.get("player_id"), {})
                r["elo_delta"] = d.get("elo")
                r["glicko2_delta"] = d.get("glicko2")
        ml = det.get("maplist") or []
        seen = set()
        ctx["maplist"] = [m for m in ml if not (m in seen or seen.add(m))]
        ctx["map_images"] = dx.get_tournament_map_images(tournament_id)
        ctx["name_slug"] = _slug(det["name"])
        ctx["schedule_start_str"] = _fmt_dt(det.get("schedule_start"))
        ctx["schedule_end_str"] = _fmt_dt(det.get("schedule_end"))
        # Recent matches in this tournament (by ID) for the matches card —
        # shown when there are no final rankings (league/group-stage events).
        ctx["matches"] = dx.get_tournament_matches(tournament_id, limit=100000)
        for m in ctx["matches"]:
            m["played_at_str"] = _fmt_dt(m.get("played_at"))
        # Per-match Elo + Glicko-2 deltas for both players (same game as the
        # tournament), for the delta columns in the matches table.
        if ctx["matches"]:
            m_deltas = dx.get_matches_rating_deltas(
                [m["match_id"] for m in ctx["matches"]], game=det.get("game") or ""
            )
            for m in ctx["matches"]:
                d = m_deltas.get(m["match_id"], {})
                m["p1_elo_delta"] = d.get(m["player1_id"], {}).get("elo")
                m["p1_glicko2_delta"] = d.get(m["player1_id"], {}).get("glicko2")
                m["p2_elo_delta"] = d.get(m["player2_id"], {}).get("elo")
                m["p2_glicko2_delta"] = d.get(m["player2_id"], {}).get("glicko2")
        ctx["canonical_name"] = det.get("name") or ""
    return templates.TemplateResponse(request, "tournament.html", ctx)


@app.get("/h2h/{p1_slug}-vs-{p2_slug}", response_class=HTMLResponse)
def h2h_slug(
    request: Request,
    p1_slug: str,
    p2_slug: str,
    p1_id: int | None = Query(None, description="Player 1 id (authoritative)"),
    p2_id: int | None = Query(None, description="Player 2 id (authoritative)"),
    p1: str = Query("", description="Player 1 name (fallback when id absent)"),
    p2: str = Query("", description="Player 2 name (fallback when id absent)"),
    game: str = Query("", description="Game name or alias (empty = all)"),
    partial: int = Query(0, ge=0, le=1, description="Return only the results partial (AJAX)"),
    sort_col: str = Query("date", description="Column to sort match history by"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$", description="Sort direction"),
):
    # Slug segments are readability only — resolution is by p1_id/p2_id in the
    # query string, so the URL works even if the slug is stale/wrong.
    # p1_id/p2_id are authoritative; fall back to name resolution when absent.
    with DataProvider() as dx:
        if p1_id is not None:
            p1_name = dx._canonical_name(p1_id)
        else:
            p1_name = p1
            p1_id = dx._player_id(p1) if p1 else None
        if p2_id is not None:
            p2_name = dx._canonical_name(p2_id)
        else:
            p2_name = p2
            p2_id = dx._player_id(p2) if p2 else None
    return _h2h_page(request, p1_name, p2_name, p1_id=p1_id, p2_id=p2_id, game=game, partial=partial, sort_col=sort_col, sort_dir=sort_dir)


@app.get("/h2h", response_class=HTMLResponse)
def h2h(
    request: Request,
    p1: str = Query("", description="Player 1 name"),
    p2: str = Query("", description="Player 2 name"),
    p1_id: int | None = Query(None, description="Player 1 id (from autocomplete; takes precedence over p1)"),
    p2_id: int | None = Query(None, description="Player 2 id (from autocomplete; takes precedence over p2)"),
    game: str = Query("", description="Game name or alias (empty = all)"),
    partial: int = Query(0, ge=0, le=1, description="Return only the results partial (AJAX)"),
    sort_col: str = Query("date", description="Column to sort match history by"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$", description="Sort direction"),
    clear: int = Query(0, ge=0, le=1),
):
    # "Clear filters" button resets all filters
    if clear:
        p1 = p2 = game = ""
        p1_id = p2_id = None
    return _h2h_page(request, p1, p2, p1_id=p1_id, p2_id=p2_id, game=game, partial=partial, sort_col=sort_col, sort_dir=sort_dir)


def _h2h_page(request: Request, p1, p2, p1_id=None, p2_id=None, game="", partial=0, sort_col="date", sort_dir="desc"):
    game = _resolve_game(game)
    with DataProvider() as dx:
        ctx = _base_context(request, dx)
        ctx["active"] = "h2h"
        ctx["game"] = game
        ctx["p1"] = p1
        ctx["p2"] = p2
        ctx["p1_id"] = p1_id
        ctx["p2_id"] = p2_id
        ctx["sort_col"] = sort_col
        ctx["sort_dir"] = sort_dir
        ctx["result"] = None
        if p1 and p2:
            # Smart match-mode auto-detection per player (no manual selector).
            p1_match = _smart_match_mode(p1, dx.name_exists)
            p2_match = _smart_match_mode(p2, dx.name_exists)
            ctx["result"] = dx.get_head_to_head(p1, p2, game=game, match=p1_match, match2=p2_match, p1_id=p1_id, p2_id=p2_id)
            # Server-side sort of the match history (consistent with matches/tournaments).
            ctx["result"]["matches"] = _sort_matches(ctx["result"]["matches"], sort_col, sort_dir)
            # Convert match datetimes for template use
            for m in ctx["result"]["matches"]:
                m["played_at_str"] = _fmt_dt(m.get("played_at"))
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
        ratings = dx.get_player_ratings(name)
        return {
            "name": ratings[0]["name"] if ratings else name,
            "ratings": ratings,
            "history": dx.get_player_history_both(name, game=game),
            "matches": dx.get_player_matches(name, game=game, limit=20),
        }


@app.get("/api/player/{player_id}/history")
def api_player_history(player_id: int, game: str = ""):
    """Rating history for the player chart, filtered by game (always all-time).
    Returns the same shape as the page's `history` JSON so the chart can be
    re-rendered in place when the game filter changes."""
    game = _resolve_game(game)
    with DataProvider() as dx:
        name = dx._canonical_name(player_id)
        hist = dx.get_player_history_both(name, game=game, limit=100000, player_id=player_id)
        # Convert datetimes to ISO strings for JSON serialization (matches page shape)
        for h in hist:
            if hasattr(h.get("played_at"), "isoformat"):
                h["played_at"] = h["played_at"].isoformat()
        return {"history": hist}


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
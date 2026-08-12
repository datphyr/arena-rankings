"""Data provider — common queries for all consumers (CLI, Discord, Twitch, Web API).

All bots and APIs use these functions. Keep return values simple (lists of dicts/tuples)
so any consumer can format them however it wants.

Also provides shared column definitions (Col builders) and game constants so
CLI, Discord bot, and future consumers all format tables consistently.

Usage:
    from src.data_provider import DataProvider, top_cols, ALL_GAMES, GAME_ALIASES

    dx = DataProvider()
    top = dx.get_top_players(game="", system="elo", limit=10)
    dx.close()
"""

import bisect
import logging
import re
from typing import Optional

from src.db_client import Database
from src.table import Col, fmt_date, fmt_rating, fmt_wr, fmt_rd, fmt_vol, fmt_tier


# Canonical tier ordering (premier > major > minor). Used so tier columns sort
# by prestige rather than alphabetically, consistently across all pages.
TIER_ORDER = {"premier": 0, "major": 1, "minor": 2}


# ─── Glicko-2 RD-stripped rating (single source of truth) ───────────────────
#
# Glicko-2 ranks/peaks use the *conservative lower bound* rating - RD rather
# than the raw rating, so players with high uncertainty (large RD) are demoted.
# This logic is used in several queries (top players, leaderboard, rank, peak).
# Keep it in ONE place so it stays consistent everywhere.

def _glicko_rank_sql(alias: str = "") -> str:
    """SQL expression for the RD-stripped Glicko-2 rating (rating - rd).

    Pass an alias (e.g. "h") when the columns are qualified in the query.
    """
    p = f"{alias}." if alias else ""
    return f"{p}rating - {p}rd"


def _glicko_rank_value(rating, rd) -> float:
    """Python value for the RD-stripped Glicko-2 rating (rating - rd).

    rd may be None (missing) — treat as 0 so the value equals the raw rating.
    """
    return rating - (rd if rd is not None else 0)


def _glicko_order_expr(system: str) -> str:
    """ORDER BY expression for a rating system.

    Glicko-2 sorts by rating - RD (conservative lower bound); Elo by raw rating.
    """
    if system == "glicko2":
        return f"{_glicko_rank_sql()} DESC"
    return "rating DESC"


def _tier_key(tier: str):
    """Sort key for a tier string: premier->major->minor, unknown tiers last."""
    if not tier:
        return (1, "")
    return (0, TIER_ORDER.get(tier.lower(), 99))


def _sort_tier(rows: list, desc: bool) -> list:
    """Sort rows by tier prestige. Premier is the highest tier value, so the
    DESCENDING direction shows premier->major->minor (premier first), and empty
    tiers always sink to the bottom in BOTH directions."""
    known = [r for r in rows if (r.get("tier") or "").strip()]
    empty = [r for r in rows if not (r.get("tier") or "").strip()]
    # desc=True -> premier first (ascending by TIER_ORDER); desc=False -> minor first.
    known.sort(key=lambda r: TIER_ORDER.get((r.get("tier") or "").lower(), 99), reverse=not desc)
    return known + empty


def _winner_from_ids(p1_id, p2_id, winner_id, p1_name, p2_name):
    """Derive the winner name from the authoritative winner_id (a player_id).

    Returns p1_name if winner_id == p1_id, p2_name if winner_id == p2_id,
    or None for draws/unknown. This is the ground truth and handles draws
    correctly (unlike score comparison).
    """
    if winner_id is not None and p1_id is not None and winner_id == p1_id:
        return p1_name
    if winner_id is not None and p2_id is not None and winner_id == p2_id:
        return p2_name
    return None


def _sort_players(rows: list, sort_col: str, sort_dir: str = "desc") -> list:
    """Sort leaderboard rows in Python by an arbitrary column.

    Used for server-side column sorting so the sort applies to ALL data,
    not just the current page's rows. sort_col is one of:
    rank, rating, peak, peak_date, rd, wins, losses, matches, win_rate, name, main_game.
    """
    if not sort_col:
        return rows
    reverse = sort_dir == "desc"

    def key(r):
        c = sort_col
        if c == "rank":
            return r.get("rank") or r.get("_rank") or 0
        if c == "win_rate":
            m = r.get("matches") or 0
            return (r.get("wins") or 0) / m if m > 0 else -1
        if c == "name":
            return (r.get("name") or "").lower()
        if c == "main_game":
            return (r.get("main_game") or "").lower()
        if c == "peak_date":
            return r.get("peak_date") or None
        # numeric columns
        v = r.get(c)
        return v if v is not None else (float("-inf") if not reverse else float("inf"))

    # None-safe: for desc, None sorts last; for asc, None sorts last too
    def sort_key(r):
        k = key(r)
        if k is None:
            return (1, 0)  # always last
        if isinstance(k, str):
            return (0, k)
        return (0, k)

    return sorted(rows, key=sort_key, reverse=reverse)


def _sort_tournaments(rows: list, sort_col: str, sort_dir: str = "desc") -> list:
    """Sort tournament rows in Python by an arbitrary column.

    Used for server-side column sorting so the sort applies to ALL data,
    not just the current page's rows. sort_col is one of:
    name, tier, game, maps, matches, players, last_match, first_match.
    """
    if not sort_col:
        return rows
    reverse = sort_dir == "desc"

    # Tier sorts by prestige (premier->major->minor) with empty tiers always last,
    # regardless of direction — so handle it separately from the generic flip.
    if sort_col == "tier":
        return _sort_tier(rows, reverse)

    def key(r):
        c = sort_col
        if c == "name":
            return (r.get("name") or "").lower()
        if c == "game":
            return (r.get("game") or "").lower()
        if c == "matches":
            return r.get("matches") or 0
        if c == "players":
            return r.get("players") or 0
        if c == "maps":
            return r.get("maps") or 0
        if c == "last_match":
            return r.get("last_match") or None
        if c == "first_match":
            return r.get("first_match") or None
        return r.get(c)

    def sort_key(r):
        k = key(r)
        if k is None:
            return (1, 0)  # always last
        if isinstance(k, str):
            return (0, k)
        if isinstance(k, tuple):
            return (0,) + k
        return (0, k)

    return sorted(rows, key=sort_key, reverse=reverse)


def _sort_matches(rows: list, sort_col: str, sort_dir: str = "desc") -> list:
    """Sort match rows in Python by an arbitrary column.

    Used for server-side column sorting so the sort applies to ALL data,
    not just the current page's rows. sort_col is one of:
    date, player1, player2, score, game, tournament, tier.
    """
    if not sort_col:
        return rows
    reverse = sort_dir == "desc"

    # Tier sorts by prestige (premier->major->minor) with empty tiers always last,
    # regardless of direction — so handle it separately from the generic flip.
    if sort_col == "tier":
        return _sort_tier(rows, reverse)

    def key(r):
        c = sort_col
        if c == "date":
            return r.get("played_at") or None
        if c == "player1":
            return (r.get("player1") or "").lower()
        if c == "player2":
            return (r.get("player2") or "").lower()
        if c == "game":
            return (r.get("game") or "").lower()
        if c == "tournament":
            return (r.get("tournament") or "").lower()
        if c == "score":
            # Sort by total score, then by score1 (winner's score) as tiebreak.
            s1 = r.get("score1") or 0
            s2 = r.get("score2") or 0
            return (s1 + s2, s1)
        return r.get(c)

    def sort_key(r):
        k = key(r)
        if k is None:
            return (1, 0)  # always last
        if isinstance(k, str):
            return (0, k)
        if isinstance(k, tuple):
            return (0,) + k
        return (0, k)

    return sorted(rows, key=sort_key, reverse=reverse)


def _glicko_period_ch_fn() -> str:
    """ClickHouse truncation function matching the configured Glicko-2 period.

    Returns a SQL expression that produces a comparable period key from played_at.
    Used in ASOF join so history query matches the same period boundaries as compute.
    """
    from config import GLICKO2_PERIOD
    if GLICKO2_PERIOD == "year":
        return "toStartOfYear"
    elif GLICKO2_PERIOD == "month":
        return "toStartOfMonth"
    elif GLICKO2_PERIOD == "week":
        return "toStartOfWeek"
    else:  # day
        return "toStartOfDay"

logger = logging.getLogger(__name__)


# ─── Game constants (shared by all consumers) ─────────────────────────────────

# All known PlusForward game names (cat_id from site)
# Games that actually have match data on PlusForward (verified 2026-08-02)
# 4 games have zero matches: Doom (9), Warsow (16), Dirty Bomb (17), Doombringer (22)
ALL_GAMES = [
    ("Blood Run", 23),
    ("Diabotical", 8),
    ("Overwatch", 13),
    ("Quake 2", 6),
    ("Quake 3 Arena", 5),
    ("Quake 3 CPMA", 21),
    ("Quake 4", 4),
    ("Quake Champions", 20),
    ("Quake Live", 3),
    ("Quake World", 7),
    ("Reflex", 10),
    ("Unreal Tournament", 15),
    ("Xonotic", 18),
]

# Short aliases → full game names (case-insensitive)
GAME_ALIASES = {
    "ql": "Quake Live",
    "quakelive": "Quake Live",
    "qc": "Quake Champions",
    "quakechampions": "Quake Champions",
    "cpm": "Quake 3 CPMA",
    "cpma": "Quake 3 CPMA",
    "q3": "Quake 3 Arena",
    "q3a": "Quake 3 Arena",
    "quake3": "Quake 3 Arena",
    "q2": "Quake 2",
    "quake2": "Quake 2",
    "q4": "Quake 4",
    "quake4": "Quake 4",
    "qw": "Quake World",
    "qworld": "Quake World",
    "quakeworld": "Quake World",
    "diabotical": "Diabotical",
    "dbt": "Diabotical",
    "br": "Blood Run",
    "bloodrun": "Blood Run",
    "ow": "Overwatch",
    "overwatch": "Overwatch",
    "ref": "Reflex",
    "reflex": "Reflex",
    "unreal": "Unreal Tournament",
    "ut": "Unreal Tournament",
    "unrealtournament": "Unreal Tournament",
    "xon": "Xonotic",
    "xonotic": "Xonotic",
}


def resolve_game(name: str) -> str:
    """Resolve a game alias to the full DB name. Case-insensitive."""
    if not name:
        return ""
    key = name.lower().strip()
    return GAME_ALIASES.get(key, name)


# ─── Column builders (shared by all consumers) ────────────────────────────────

def top_cols(is_glicko: bool, is_combined: bool) -> list[Col]:
    """Column definitions for the top/leaderboard table."""
    cols = [
        Col("#", ">", key=lambda r: r["_rank"]),
        Col("Player", "<", key=lambda r: r["name"]),
    ]
    if is_combined:
        cols.append(Col("Game", "<", key=lambda r: r.get("main_game") or ""))
    cols.append(Col("Rating", ">", key=lambda r: r["rating"], fmt=fmt_rating))
    cols.append(Col("Peak", ">", key=lambda r: r.get("peak"), fmt=fmt_rating))
    cols.append(Col("Peak Date", ">", key=lambda r: r.get("peak_date"), fmt=fmt_date))
    if is_glicko:
        cols.append(Col("RD", ">", key=lambda r: r.get("rd"), fmt=fmt_rd))
        cols.append(Col("Vol", ">", key=lambda r: r.get("vol"), fmt=fmt_vol))
    cols.extend([
        Col("W", ">", key=lambda r: r["wins"]),
        Col("L", ">", key=lambda r: r["losses"]),
        Col("M", ">", key=lambda r: r["matches"]),
        Col("WR", ">", key=lambda r: fmt_wr(r["wins"], r["matches"])),
        Col("First", ">", key=lambda r: r.get("first_match_date"), fmt=fmt_date),
        Col("Last", ">", key=lambda r: r.get("last_match_date"), fmt=fmt_date),
    ])
    return cols


def player_cols_elo() -> list[Col]:
    """Column definitions for the player Elo ratings table (no RD/Vol)."""
    return [
        Col("Game", "<", key=lambda r: r.get("game") or ""),
        Col("Rating", ">", key=lambda r: r["rating"], fmt=fmt_rating),
        Col("Peak", ">", key=lambda r: r.get("peak"), fmt=fmt_rating),
        Col("Peak Date", ">", key=lambda r: r.get("peak_date"), fmt=fmt_date),
        Col("Rank", ">", key=lambda r: r.get("rank", "—")),
        Col("W", ">", key=lambda r: r["wins"]),
        Col("L", ">", key=lambda r: r["losses"]),
        Col("M", ">", key=lambda r: r["matches"]),
        Col("WR", ">", key=lambda r: fmt_wr(r["wins"], r["matches"])),
        Col("First", ">", key=lambda r: r.get("first_match_date"), fmt=fmt_date),
        Col("Last", ">", key=lambda r: r.get("last_match_date"), fmt=fmt_date),
    ]


def player_cols_glicko() -> list[Col]:
    """Column definitions for the player Glicko-2 ratings table (with RD/Vol)."""
    return [
        Col("Game", "<", key=lambda r: r.get("game") or ""),
        Col("Rating", ">", key=lambda r: r["rating"], fmt=fmt_rating),
        Col("Peak", ">", key=lambda r: r.get("peak"), fmt=fmt_rating),
        Col("Peak Date", ">", key=lambda r: r.get("peak_date"), fmt=fmt_date),
        Col("RD", ">", key=lambda r: r.get("rd"), fmt=fmt_rd),
        Col("Vol", ">", key=lambda r: r.get("vol"), fmt=fmt_vol),
        Col("Rank", ">", key=lambda r: r.get("rank", "—")),
        Col("W", ">", key=lambda r: r["wins"]),
        Col("L", ">", key=lambda r: r["losses"]),
        Col("M", ">", key=lambda r: r["matches"]),
        Col("WR", ">", key=lambda r: fmt_wr(r["wins"], r["matches"])),
        Col("First", ">", key=lambda r: r.get("first_match_date"), fmt=fmt_date),
        Col("Last", ">", key=lambda r: r.get("last_match_date"), fmt=fmt_date),
    ]


def history_cols(is_glicko: bool) -> list[Col]:
    """Column definitions for the rating history table (single system)."""
    cols = [
        Col("Date", ">", key=lambda r: r.get("played_at"), fmt=fmt_date),
        Col("Match", ">", key=lambda r: r["match_id"]),
        Col("Rating", ">", key=lambda r: r["rating"], fmt=fmt_rating),
    ]
    if is_glicko:
        cols.append(Col("RD", ">", key=lambda r: r.get("rd"), fmt=fmt_rd))
        cols.append(Col("Vol", ">", key=lambda r: r.get("vol"), fmt=fmt_vol))
    cols.extend([
        Col("W", ">", key=lambda r: r["wins"]),
        Col("L", ">", key=lambda r: r["losses"]),
        Col("M", ">", key=lambda r: r["matches"]),
    ])
    return cols


def history_cols_both() -> list[Col]:
    """Column definitions for the rating history table (both systems per row)."""
    return [
        Col("Date", ">", key=lambda r: r.get("played_at"), fmt=fmt_date),
        Col("Match", ">", key=lambda r: r["match_id"]),
        Col("Elo", ">", key=lambda r: r.get("elo"), fmt=fmt_rating),
        Col("Glicko-2", ">", key=lambda r: r.get("glicko2"), fmt=fmt_rating),
        Col("RD", ">", key=lambda r: r.get("rd"), fmt=fmt_rd),
        Col("Vol", ">", key=lambda r: r.get("vol"), fmt=fmt_vol),
        Col("W", ">", key=lambda r: r["wins"]),
        Col("L", ">", key=lambda r: r["losses"]),
        Col("M", ">", key=lambda r: r["matches"]),
    ]


def h2h_cols() -> list[Col]:
    """Column definitions for the head-to-head table."""
    return [
        Col("Date", ">", key=lambda m: m.get("played_at"), fmt=fmt_date),
        Col("Game", "<", key=lambda m: m.get("game") or "—"),
        Col("Tournament", "<", key=lambda m: m.get("tournament") or "—"),
        Col("Tier", "<", key=lambda m: m.get("tier"), fmt=fmt_tier),
        Col("Score", ">", key=lambda m: m["score"]),
        Col("Winner", "<", key=lambda m: m["winner"]),
    ]


def matches_cols() -> list[Col]:
    """Column definitions for the recent matches table."""
    return [
        Col("Date", ">", key=lambda m: m.get("played_at"), fmt=fmt_date),
        Col("Player1", "<", key=lambda m: m["player1"]),
        Col("Score", ">", key=lambda m: m["score"]),
        Col("Player2", "<", key=lambda m: m["player2"]),
        Col("Game", "<", key=lambda m: m.get("game") or "—"),
        Col("Tournament", "<", key=lambda m: m.get("tournament") or "—"),
        Col("Tier", "<", key=lambda m: m.get("tier"), fmt=fmt_tier),
    ]


def player_matches_cols(resolved: str) -> list[Col]:
    """Column definitions for a player's match history table."""
    def _opponent(m):
        return m['player2'] if m['player1'] == resolved else m['player1']

    def _score(m):
        if m['player1'] == resolved:
            return m['score']
        parts = m['score'].split('-')
        return f"{parts[1]}-{parts[0]}" if len(parts) == 2 else m['score']

    def _result(m):
        return "WIN" if m['winner'] == resolved else "LOSS"

    return [
        Col("Date", ">", key=lambda m: m.get("played_at"), fmt=fmt_date),
        Col("Opponent", "<", key=_opponent),
        Col("Score", ">", key=_score),
        Col("Result", ">", key=_result),
        Col("Game", "<", key=lambda m: m.get("game") or "—"),
        Col("Tournament", "<", key=lambda m: m.get("tournament") or "—"),
        Col("Tier", "<", key=lambda m: m.get("tier"), fmt=fmt_tier),
    ]


def games_cols(db_games: set, alias_map: dict) -> list[Col]:
    """Column definitions for the games listing table."""
    return [
        Col("Game", "<", key=lambda r: r["name"]),
        Col("Status", ">", key=lambda r: "✓ data" if r["name"] in db_games else "—"),
        Col("Aliases", "<", key=lambda r: ", ".join(sorted(alias_map.get(r["name"], []), key=len))),
    ]


def tournaments_cols() -> list[Col]:
    """Column definitions for the tournaments listing table."""
    return [
        Col("Tournament", "<", key=lambda r: r["name"]),
        Col("Game", "<", key=lambda r: r["game"]),
        Col("Tier", "<", key=lambda r: r["tier"], fmt=fmt_tier),
        Col("Matches", ">", key=lambda r: r["matches"]),
        Col("First", ">", key=lambda r: r.get("first_match"), fmt=fmt_date),
        Col("Last", ">", key=lambda r: r.get("last_match"), fmt=fmt_date),
    ]


def tier_stats_cols() -> list[Col]:
    """Column definitions for the tier distribution table."""
    return [
        Col("Tier", "<", key=lambda r: r["tier"], fmt=fmt_tier),
        Col("Tournaments", ">", key=lambda r: r["tournaments"]),
        Col("Matches", ">", key=lambda r: r["matches"]),
    ]


# Module-level caches shared across DataProvider instances (and thus across
# requests in the long-running web process). The underlying data is static
# (imported by a separate process), so caching canonical names / countries /
# player-id lookups / the games list here avoids re-scanning matches on every
# request. These are small (one entry per player_id) and bounded by the number
# of players.
_CANONICAL_CACHE: dict[int, str] = {}
_COUNTRY_CACHE: dict[int, str] = {}
_PLAYER_ID_CACHE: dict[str, Optional[int]] = {}
_GAMES_CACHE: Optional[list[str]] = None
_PEAK_OVERALL_CACHE: dict[tuple, dict] = {}
_STATS_CACHE: Optional[dict] = None
_TOURNAMENT_STATS_CACHE: Optional[dict] = None
_MAIN_GAMES_CACHE: dict[str, dict[int, str]] = {}
_MAP_IMAGE_CACHE: dict[int, str] = {}  # map_id -> image path (from maps table)
_MAP_NAME_CACHE: dict[str, str] = {}  # canonical map name -> image path


class DataProvider:
    """Common data access layer for all consumers."""

    def __init__(self, db: Optional[Database] = None):
        self.db = db or Database()
        self._owns_db = db is None
        # Caches of canonical (most-used) display name / country / resolved
        # player_id / games list. Shared module-level so they persist across
        # requests (the data is static).
        self._canonical_cache: dict[int, str] = _CANONICAL_CACHE
        self._country_cache: dict[int, str] = _COUNTRY_CACHE
        self._player_id_cache: dict[str, Optional[int]] = _PLAYER_ID_CACHE
        self._games_cache: Optional[list[str]] = _GAMES_CACHE

    def close(self):
        if self._owns_db:
            self.db.close()

    # --- Canonical display names ---

    def _canonical_names(self, player_ids: list[int]) -> dict[int, str]:
        """Return the canonical (most-used) display name for each player_id.

        The canonical name is the most frequently occurring spelling of the
        player's name across all matches (player1_name + player2_name counts).
        This ensures a single stable display name per player_id, so the same
        player never shows up under multiple spellings in autocomplete or
        leaderboards. Falls back to the players table name, then to a
        player_<id> placeholder.
        """
        missing = [pid for pid in player_ids if pid not in self._canonical_cache]
        if missing:
            # Most-used spelling per player_id from matches (both sides).
            rows = self.db.client.execute(
                """
                SELECT pid, argMax(name, cnt) AS name
                FROM (
                    SELECT player1_id AS pid, player1_name AS name, count() AS cnt
                    FROM matches
                    WHERE player1_id IN %(ids)s AND player1_name != ''
                    GROUP BY player1_id, player1_name
                    UNION ALL
                    SELECT player2_id AS pid, player2_name AS name, count() AS cnt
                    FROM matches
                    WHERE player2_id IN %(ids)s AND player2_name != ''
                    GROUP BY player2_id, player2_name
                )
                GROUP BY pid
                """,
                {"ids": tuple(missing)},
            )
            found = {r[0]: r[1] for r in rows}
            # Fallback: players table for ids with no matches.
            still = [pid for pid in missing if pid not in found]
            if still:
                prows = self.db.client.execute(
                    "SELECT player_id, name FROM players FINAL WHERE player_id IN %(ids)s",
                    {"ids": tuple(still)},
                )
                for pid, name in prows:
                    if name:
                        found[pid] = name
            for pid in missing:
                self._canonical_cache[pid] = found.get(pid, f"player_{pid}")
        return {pid: self._canonical_cache[pid] for pid in player_ids}

    def _canonical_name(self, player_id: int) -> str:
        """Canonical (most-used) display name for a single player_id."""
        return self._canonical_names([player_id])[player_id]

    def _countries(self, player_ids: list[int]) -> dict[int, str]:
        """Return the ISO country code for each player_id (from the players
        table). Missing/unknown players map to '' (no flag). Cached."""
        missing = [pid for pid in player_ids if pid not in self._country_cache]
        if missing:
            rows = self.db.client.execute(
                "SELECT player_id, country FROM players FINAL WHERE player_id IN %(ids)s",
                {"ids": tuple(missing)},
            )
            for pid, country in rows:
                self._country_cache[pid] = country or ""
            for pid in missing:
                self._country_cache.setdefault(pid, "")
        return {pid: self._country_cache.get(pid, "") for pid in player_ids}

    def _country(self, player_id: int) -> str:
        """ISO country code for a single player_id ('' if unknown)."""
        return self._countries([player_id])[player_id]

    # --- Map images (from the maps lookup table) ---

    def _load_map_images(self):
        """Load the full maps table (map_id -> image, name -> image) once."""
        if not _MAP_IMAGE_CACHE:
            rows = self.db.client.execute(
                "SELECT map_id, name, image FROM maps FINAL WHERE image != ''"
            )
            for mid, name, image in rows:
                _MAP_IMAGE_CACHE[mid] = image
                if name:
                    _MAP_NAME_CACHE.setdefault(name, image)

    def map_image_by_id(self, map_id: int) -> str:
        """Image path for a map_id ('' if unknown). From the maps table."""
        self._load_map_images()
        return _MAP_IMAGE_CACHE.get(map_id, "")

    def map_image_by_name(self, name: str) -> str:
        """Image path for a canonical map name ('' if unknown)."""
        self._load_map_images()
        return _MAP_NAME_CACHE.get(name, "")

    def map_images_by_ids(self, map_ids: list[int]) -> dict[int, str]:
        """{map_id: image} for the given ids ('' for unknown)."""
        self._load_map_images()
        return {mid: _MAP_IMAGE_CACHE.get(mid, "") for mid in map_ids}

    def map_images_by_names(self, names: list[str]) -> dict[str, str]:
        """{name: image} for the given canonical names ('' for unknown)."""
        self._load_map_images()
        return {n: _MAP_NAME_CACHE.get(n, "") for n in names}

    def _aliases(self, player_id: int) -> list[str]:
        """All distinct name spellings for a player_id, most-used first,
        EXCLUDING the canonical (most-used) name.

        Returns the least-used aliases (e.g. case variants, old tags) so the
        UI can show them dimmed next to the main name. Empty list if the
        player has only ever used one spelling.
        """
        rows = self.db.client.execute(
            """
            SELECT name, count() AS cnt
            FROM (
                SELECT player1_name AS name FROM matches WHERE player1_id = %(pid)s AND player1_name != ''
                UNION ALL
                SELECT player2_name AS name FROM matches WHERE player2_id = %(pid)s AND player2_name != ''
            )
            GROUP BY name
            ORDER BY cnt DESC, name
            """,
            {"pid": player_id},
        )
        canon = self._canonical_name(player_id)
        return [r[0] for r in rows if r[0] != canon]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _resolve_name(self, name: str) -> str:
        """Resolve a player name case-insensitively to its canonical display name.

        Returns the canonical (most-used) spelling for the matching player_id,
        or the input unchanged if no player matches. This ensures that any
        spelling of a player's name (e.g. 'pthy' vs 'Pthy') resolves to the
        same single display name.
        """
        pid = self._player_id(name)
        if pid is not None:
            return self._canonical_name(pid)
        return name

    def name_exists(self, name: str) -> bool:
        """True if `name` matches an exact known player name (case-insensitive)."""
        return self._player_id(name) is not None

    def _player_id(self, name: str) -> Optional[int]:
        """Resolve the player_id for a player name, or None.

        Prefers an EXACT-case match first (so distinct players that differ only
        by case, e.g. 'pavel' (836) vs 'Pavel' (9152), stay distinct), then
        falls back to case-insensitive matching (so 'PTHY' resolves to 'pthy').
        Prefers player_ratings (players with ratings), then the matches table.
        """
        if name in self._player_id_cache:
            return self._player_id_cache[name]
        # 1. Exact-case match in player_ratings.
        row = self.db.client.execute(
            "SELECT player_id FROM player_ratings FINAL WHERE player_name = %(n)s LIMIT 1",
            {"n": name},
        )
        if row:
            self._player_id_cache[name] = row[0][0]
            return row[0][0]
        # 2. Case-insensitive match in player_ratings.
        row = self.db.client.execute(
            "SELECT player_id FROM player_ratings FINAL WHERE lowerUTF8(player_name) = lowerUTF8(%(n)s) LIMIT 1",
            {"n": name},
        )
        if row:
            self._player_id_cache[name] = row[0][0]
            return row[0][0]
        # 3. Exact-case match in matches (players without ratings yet).
        row = self.db.client.execute(
            "SELECT player1_id FROM matches WHERE player1_name = %(n)s LIMIT 1",
            {"n": name},
        )
        if row:
            self._player_id_cache[name] = row[0][0]
            return row[0][0]
        row = self.db.client.execute(
            "SELECT player2_id FROM matches WHERE player2_name = %(n)s LIMIT 1",
            {"n": name},
        )
        if row:
            self._player_id_cache[name] = row[0][0]
            return row[0][0]
        # 4. Case-insensitive match in matches.
        row = self.db.client.execute(
            "SELECT player1_id FROM matches WHERE lowerUTF8(player1_name) = lowerUTF8(%(n)s) LIMIT 1",
            {"n": name},
        )
        if row:
            self._player_id_cache[name] = row[0][0]
            return row[0][0]
        row = self.db.client.execute(
            "SELECT player2_id FROM matches WHERE lowerUTF8(player2_name) = lowerUTF8(%(n)s) LIMIT 1",
            {"n": name},
        )
        if row:
            self._player_id_cache[name] = row[0][0]
            return row[0][0]
        self._player_id_cache[name] = None
        return None

    # --- Games ---

    def get_games(self) -> list[str]:
        """Return list of game names (excluding all-games aggregate)."""
        global _GAMES_CACHE
        if _GAMES_CACHE is None:
            rows = self.db.client.execute(
                "SELECT DISTINCT game_name FROM matches WHERE game_name != '' ORDER BY game_name"
            )
            _GAMES_CACHE = [r[0] for r in rows]
        self._games_cache = _GAMES_CACHE
        return self._games_cache

    def autocomplete(self, kind: str, q: str, limit: int = 20) -> list[dict] | list[str]:
        """Return matching names for autocomplete dropdowns.

        kind is 'player' or 'tournament'. Matching is case-insensitive and
        partial (substring), so typing 'ra' matches 'rapha'. Returns up to
        `limit` entries.

        For 'player', returns a list of {"name", "id"} dicts — one per
        distinct name spelling (no dedup by player_id, so all unique aliases
        of the same player appear, e.g. 'davjs', 'davis', 'lat',
        'lateral0lz' all show up). The id is the player_id for that spelling.

        For 'tournament', returns a list of plain name strings.
        """
        q = (q or "").strip()
        if not q:
            return []
        like = f"%{q}%"
        if kind == "player":
            # Collect distinct (player_id, name) pairs whose name matches the
            # substring — one row per distinct spelling, so aliases of the
            # same player are NOT collapsed into a single canonical entry.
            rows = self.db.client.execute(
                """
                SELECT DISTINCT player_id, player_name
                FROM player_ratings FINAL
                WHERE lowerUTF8(player_name) LIKE lowerUTF8(%(q)s)
                LIMIT %(lim)s
                """,
                {"q": like, "lim": limit},
            )
            entries = [{"name": r[1], "id": r[0]} for r in rows]
            if len(entries) < limit:
                # Also pull distinct spellings from matches (both sides) —
                # catches aliases that only ever appear in match rows (e.g.
                # 'davjs', 'lateral0lz') and are absent from player_ratings.
                extra = self.db.client.execute(
                    """
                    SELECT DISTINCT pid, name FROM (
                        SELECT player1_id AS pid, player1_name AS name FROM matches
                        WHERE lowerUTF8(player1_name) LIKE lowerUTF8(%(q)s)
                        UNION ALL
                        SELECT player2_id AS pid, player2_name AS name FROM matches
                        WHERE lowerUTF8(player2_name) LIKE lowerUTF8(%(q)s)
                    )
                    LIMIT %(lim)s
                    """,
                    {"q": like, "lim": limit},
                )
                seen = {(e["id"], e["name"]) for e in entries}
                for pid, name in extra:
                    if (pid, name) not in seen:
                        entries.append({"name": name, "id": pid})
                        seen.add((pid, name))
                        if len(entries) >= limit:
                            break
            entries.sort(key=lambda e: e["name"].lower())
            return entries[:limit]
        if kind == "tournament":
            rows = self.db.client.execute(
                """
                SELECT DISTINCT tournament_name FROM matches
                WHERE tournament_name != '' AND lowerUTF8(tournament_name) LIKE lowerUTF8(%(q)s)
                ORDER BY tournament_name LIMIT %(lim)s
                """,
                {"q": like, "lim": limit},
            )
            return [r[0] for r in rows]
        return []

    # --- Tournaments ---

    def get_tournaments(self, tier: str = "", game: str = "", limit: int = 50, offset: int = 0, sort_col: str = "", sort_dir: str = "desc") -> list[dict]:
        """List tournaments, optionally filtered by tier and/or game.

        When sort_col is set, ALL matching rows are fetched and sorted in
        Python so the sort applies to the full dataset, not just the page.
        """
        params = {"lim": limit, "off": offset}
        conds = ["t.name != ''"]
        if tier:
            params["tier"] = tier
            conds.append("t.tier = %(tier)s")
        if game:
            params["game"] = game
            conds.append("m.game_name = %(game)s")
        where = " AND ".join(conds)
        fetch_limit = 100000 if sort_col else "%(lim)s"
        rows = self.db.client.execute(
            f"""
            SELECT t.tournament_id, t.name, t.tier,
                   min(m.played_at) AS first_match, max(m.played_at) AS last_match,
                   any(m.game_name) AS game
            FROM tournaments t FINAL
            LEFT JOIN matches m ON m.tournament_id = t.tournament_id
            WHERE {where}
            GROUP BY t.tournament_id, t.name, t.tier
            ORDER BY last_match DESC
            LIMIT {fetch_limit} OFFSET %(off)s
            """,
            params,
        )
        # Count matches per tournament
        t_ids = [r[0] for r in rows]
        match_counts = {}
        player_counts = {}
        map_counts = {}
        if t_ids:
            mc_params = {"ids": tuple(t_ids)}
            mc_query = (
                "SELECT tournament_id, count() FROM matches "
                "WHERE tournament_id IN %(ids)s"
            )
            if game:
                mc_params["game"] = game
                mc_query += " AND game_name = %(game)s"
            mc_query += " GROUP BY tournament_id"
            mc_rows = self.db.client.execute(mc_query, mc_params)
            match_counts = {r[0]: r[1] for r in mc_rows}
            # Count distinct players per tournament (union of both columns)
            pc_params = {"ids": tuple(t_ids)}
            pc_where = ""
            if game:
                pc_params["game"] = game
                pc_where = " AND game_name = %(game)s"
            pc_query = (
                "SELECT tournament_id, uniqExact(pid) FROM ("
                "SELECT tournament_id, player1_id AS pid FROM matches WHERE tournament_id IN %(ids)s" + pc_where + " "
                "UNION ALL "
                "SELECT tournament_id, player2_id AS pid FROM matches WHERE tournament_id IN %(ids)s" + pc_where + " "
                ") GROUP BY tournament_id"
            )
            pc_rows = self.db.client.execute(pc_query, pc_params)
            player_counts = {r[0]: r[1] for r in pc_rows}
            # Count distinct maps per tournament from the tournament's own
            # maplist (authoritative per-tournament map rotation; arrayDistinct
            # removes repeated maps from group-stage rotations). Fall back to 0
            # for tournaments with no maplist.
            mcp_params = {"ids": tuple(t_ids)}
            mcp_query = (
                "SELECT tournament_id, length(arrayDistinct(any(maplist))) "
                "FROM tournaments FINAL "
                "WHERE tournament_id IN %(ids)s "
                "GROUP BY tournament_id"
            )
            mcp_rows = self.db.client.execute(mcp_query, mcp_params)
            map_counts = {r[0]: r[1] for r in mcp_rows}
        tournaments = [
            {
                "tournament_id": r[0],
                "name": r[1],
                "tier": r[2],
                "first_match": r[3],
                "last_match": r[4],
                "matches": match_counts.get(r[0], 0),
                "players": player_counts.get(r[0], 0),
                "maps": map_counts.get(r[0], 0),
                "game": r[5] or "",
            }
            for r in rows
        ]
        if sort_col:
            tournaments = _sort_tournaments(tournaments, sort_col, sort_dir)
            tournaments = tournaments[offset:offset + limit]
        return tournaments

    def count_tournaments(self, tier: str = "", game: str = "") -> int:
        """Total number of tournaments matching the given filters (for pagination)."""
        params = {}
        conds = ["t.name != ''"]
        if tier:
            params["tier"] = tier
            conds.append("t.tier = %(tier)s")
        if game:
            params["game"] = game
            conds.append("m.game_name = %(game)s")
        where = " AND ".join(conds)
        rows = self.db.client.execute(
            f"""
            SELECT count()
            FROM (
                SELECT t.tournament_id
                FROM tournaments t FINAL
                LEFT JOIN matches m ON m.tournament_id = t.tournament_id
                WHERE {where}
                GROUP BY t.tournament_id
            )
            """,
            params,
        )
        return rows[0][0] if rows else 0

    def get_tournament_stats(self) -> dict:
        """Tier distribution stats. Cached at module level — static between imports."""
        global _TOURNAMENT_STATS_CACHE
        if _TOURNAMENT_STATS_CACHE is not None:
            return _TOURNAMENT_STATS_CACHE
        rows = self.db.client.execute(
            """
            SELECT t.tier, count(DISTINCT m.tournament_id) AS tournaments, count() AS matches
            FROM tournaments t FINAL
            INNER JOIN matches m ON m.tournament_id = t.tournament_id
            WHERE t.name != ''
            GROUP BY t.tier
            ORDER BY matches DESC
            """
        )
        result = {
            "tiers": [
                {"tier": r[0], "tournaments": r[1], "matches": r[2]}
                for r in rows
            ]
        }
        _TOURNAMENT_STATS_CACHE = result
        return result

    def get_tournament_details(self, tournament_id: int) -> Optional[dict]:
        """Return parsed tournament metadata (game, prize, formats, schedule,
        maplist, rankings) for a single tournament by ID. None if missing."""
        det = self.db.get_tournament_details(tournament_id)
        if det is None:
            return None
        # Attach match count + first/last match date for the header.
        mrow = self.db.client.execute(
            "SELECT count(), min(played_at), max(played_at) "
            "FROM matches WHERE tournament_id = %(t)s",
            {"t": tournament_id},
        )
        det["matches_count"] = mrow[0][0] if mrow else 0
        det["first_match"] = mrow[0][1] if mrow else None
        det["last_match"] = mrow[0][2] if mrow else None
        # Distinct player count (union of both player columns), same as the
        # tournaments table's "Players" column.
        prow = self.db.client.execute(
            "SELECT uniqExact(pid) FROM ("
            "SELECT player1_id AS pid FROM matches WHERE tournament_id = %(t)s "
            "UNION ALL "
            "SELECT player2_id AS pid FROM matches WHERE tournament_id = %(t)s "
            ") ",
            {"t": tournament_id},
        )
        det["players_count"] = prow[0][0] if prow else 0
        return det

    def get_tournament_map_images(self, tournament_id: int) -> dict:
        """Return {map_name: image_path} for maps in this tournament.

        Images come from the maps lookup table (by canonical name). Names are
        gathered from the tournament's match_maps rows; for tournaments whose
        matches have no map data, fall back to the tournament's own maplist
        (parsed from raw_html). Any name still missing falls back to data-image
        attributes extracted from the tournament's raw_html.
        """
        # Canonical map names for this tournament (from its matches' maps).
        names = [r[0] for r in self.db.client.execute(
            """
            SELECT DISTINCT mm.map_name FROM match_maps mm
            JOIN matches m ON m.match_id = mm.match_id
            WHERE m.tournament_id = %(t)s AND mm.map_name != ''
            """,
            {"t": tournament_id},
        )]
        # Fall back to the tournament's own maplist (parsed from raw_html) when
        # its matches have no map data.
        if not names:
            det = self.db.get_tournament_details(tournament_id)
            if det and det.get("maplist"):
                names = list(det["maplist"])
        if not names:
            return {}
        self._load_map_images()
        images = {n: _MAP_NAME_CACHE.get(n, "") for n in names}
        # Fall back to raw_html for names missing from the maps table.
        missing = [n for n in names if not images[n]]
        if missing:
            rows = self.db.client.execute(
                "SELECT raw_html FROM tournaments WHERE tournament_id = %(t)s",
                {"t": tournament_id},
            )
            if rows and rows[0][0]:
                for m in re.finditer(
                    r'data-image="([^"]+)"[^>]*data-name="([^"]+)"', rows[0][0]
                ):
                    if m.group(2) in missing:
                        images[m.group(2)] = m.group(1)
        return images

    def get_tournament_matches(self, tournament_id: int, limit: int = 20) -> list[dict]:
        """Recent matches belonging to a tournament (by ID, unambiguous even
        when tournament names collide). Newest first."""
        rows = self.db.client.execute(
            """
            SELECT m.match_id, m.player1_name, m.player2_name, m.player1_score, m.player2_score,
                   m.player1_id, m.player2_id, m.winner_id, m.game_name, m.stage_name, m.played_at, t.tier
            FROM matches m
            LEFT JOIN tournaments t ON m.tournament_id = t.tournament_id
            WHERE m.tournament_id = %(t)s
            ORDER BY m.played_at ASC
            LIMIT %(lim)s
            """,
            {"t": tournament_id, "lim": limit},
        )
        # Batch-fetch country codes for all involved players (from players table).
        pids = {r[5] for r in rows} | {r[6] for r in rows}
        countries = self._countries(list(pids))
        return [
            {
                "match_id": r[0],
                "player1": r[1],
                "player2": r[2],
                "score1": r[3],
                "score2": r[4],
                "player1_id": r[5],
                "player2_id": r[6],
                "player1_country": countries.get(r[5], ""),
                "player2_country": countries.get(r[6], ""),
                "winner": r[1] if r[7] == r[5] else r[2] if r[7] == r[6] else "",
                "game": r[8],
                "stage": r[9],
                "played_at": r[10],
                "tier": r[11],
            }
            for r in rows
        ]

    def get_tournament_rating_deltas(self, tournament_id: int) -> dict:
        """Elo + Glicko-2 rating change for each player in a tournament's final
        standings, over the tournament's own time window.

        For each player: delta = rating at/before the tournament's last match
        minus rating just before the tournament's first match. Both systems are
        read from rating_history (same game as the tournament). Players with no
        snapshots on either side get None for that system.

        Returns dict keyed by player_id: {player_id: {'elo': float|None,
        'glicko2': float|None}}.
        """
        det = self.db.get_tournament_details(tournament_id)
        if det is None:
            return {}
        game = det.get("game") or ""
        # Tournament time window from the matches table.
        mrow = self.db.client.execute(
            "SELECT min(played_at), max(played_at) FROM matches WHERE tournament_id = %(t)s",
            {"t": tournament_id},
        )
        if not mrow or not mrow[0][0] or not mrow[0][1]:
            return {}
        first_ts, last_ts = mrow[0][0], mrow[0][1]
        # Players in the final standings.
        try:
            import json as _json
            standings = _json.loads(det.get("rankings") or "[]")
        except Exception:
            standings = []
        pids = [r.get("player_id") for r in standings if r.get("player_id")]
        if not pids:
            return {}

        out: dict[int, dict[str, float | None]] = {p: {"elo": None, "glicko2": None} for p in pids}
        for sys in ("elo", "glicko2"):
            # Fetch each standings player's full history up to the last match in
            # ONE query, then derive both the start and end snapshots in Python.
            # This replaces the previous 5 queries (start + fallback_start + end
            # per system) with 2. The history is small (standings players only).
            hist_rows = self.db.client.execute(
                """
                SELECT player_id, played_at, rating
                FROM rating_history
                WHERE player_id IN %(ids)s AND rating_system = %(sys)s
                  AND game_name = %(game)s AND played_at <= %(last)s
                ORDER BY player_id, played_at, match_id
                """,
                {"ids": tuple(pids), "sys": sys, "game": game, "last": last_ts},
            )
            hist: dict[int, list[tuple]] = {}
            for pid, ts, rating in hist_rows:
                hist.setdefault(pid, []).append((ts, rating))
            for pid in pids:
                rows = hist.get(pid, [])
                if not rows:
                    continue
                # End rating: most recent snapshot at/before the last match.
                end_rating = rows[-1][1]
                # Start rating: most recent snapshot strictly before the first
                # match; if none (history begins inside the window), use the
                # earliest in-window snapshot as the baseline.
                idx = bisect.bisect_left(rows, (first_ts,))
                start_rating = rows[idx - 1][1] if idx > 0 else rows[0][1]
                out[pid][sys] = round(end_rating - start_rating, 1)
        return out

    def get_matches_rating_deltas(self, match_ids: list[int], game: str = "") -> dict:
        """Per-match Elo + Glicko-2 rating change for each player in a set of
        matches (same game as the tournament).

        For each match and each of its two players, delta = the player's rating
        snapshot at that match minus their rating snapshot at the previous match
        (same game + system). Winner gains, loser loses, so the two deltas are
        roughly symmetric. Players with no prior snapshot get None.

        Returns dict keyed by match_id: {match_id: {player_id: {'elo': float|None,
        'glicko2': float|None}}}.
        """
        if not match_ids:
            return {}
        # Match timestamps + players.
        mrows = self.db.client.execute(
            """
            SELECT match_id, player1_id, player2_id, played_at
            FROM matches
            WHERE match_id IN %(ids)s
            """,
            {"ids": tuple(match_ids)},
        )
        if not mrows:
            return {}
        match_info = {r[0]: (r[1], r[2], r[3]) for r in mrows}
        pids = {p for _, (p1, p2, _) in match_info.items() for p in (p1, p2)}
        # Only snapshots at or before the latest match timestamp are needed to
        # compute each match's previous snapshot — anything after the last match
        # in the set is irrelevant. Filtering here cuts the history fetch
        # dramatically (e.g. 32k -> 9k rows for a 446-match tournament).
        max_ts = max((ts for _, (_, _, ts) in match_info.items()), default=None)
        out: dict[int, dict[int, dict[str, float | None]]] = {
            mid: {p: {"elo": None, "glicko2": None} for p in (p1, p2)}
            for mid, (p1, p2, _) in match_info.items()
        }
        for sys in ("elo", "glicko2"):
            # Fetch each player's full history up to max_ts in ONE query. This
            # includes the match-tagged snapshots (all have played_at <= max_ts),
            # so we derive both the rating-at-each-match map and the previous-
            # snapshot history from the same rows — eliminating the separate
            # at_rows query per system.
            prev_rows = self.db.client.execute(
                """
                SELECT player_id, played_at, rating, match_id
                FROM rating_history
                WHERE player_id IN %(ids)s AND rating_system = %(sys)s
                  AND game_name = %(game)s AND played_at <= %(max)s
                ORDER BY player_id, played_at, match_id
                """,
                {"ids": tuple(pids), "sys": sys, "game": game, "max": max_ts},
            )
            at_map: dict[int, dict[int, float]] = {}
            hist: dict[int, list[tuple]] = {}
            for pid, ts, rating, mid in prev_rows:
                hist.setdefault(pid, []).append((ts, rating))
                if mid in match_info:
                    at_map.setdefault(mid, {})[pid] = rating
            for mid, (p1, p2, played_at) in match_info.items():
                prev_map: dict[int, float] = {}
                for pid in (p1, p2):
                    h = hist.get(pid, [])
                    idx = bisect.bisect_left(h, (played_at,))
                    prev_map[pid] = h[idx - 1][1] if idx > 0 else None
                for pid in (p1, p2):
                    cur = at_map.get(mid, {}).get(pid)
                    prev = prev_map.get(pid)
                    if cur is not None and prev is None:
                        # Debut match: no prior snapshot. Use the initial rating
                        # baseline (both Elo and Glicko-2 start new players at
                        # 1500.0) so we still report the change from that match.
                        prev = 1500.0
                    if cur is not None and prev is not None:
                        out[mid][pid][sys] = round(cur - prev, 1)
        return out

    # --- Leaderboards ---

    def _fetch_peaks(self, player_ids: list[int], system: str) -> dict:
        """Fetch peak rating + date for a batch of players from rating_history.

        For glicko2 the returned peak is the conservative lower bound
        (peak - rd_at_peak), i.e. RD is removed using the RD from the date the
        peak was reached — consistent with the leaderboard sort and the home
        page plaque. The RD at the peak date is returned alongside so callers
        can display it.

        Returns dict: {(player_id, game_name): (peak_rating, peak_date, rd_at_peak)}
        """
        if not player_ids:
            return {}
        if system == "glicko2":
            peak_rows = self.db.client.execute(
                """
                SELECT player_id, game_name,
                       max(rating) - argMax(rd, rating) AS peak,
                       argMax(played_at, rating) AS peak_date,
                       argMax(rd, rating) AS rd_at_peak
                FROM rating_history
                WHERE rating_system = %(rs)s AND player_id IN %(ids)s
                GROUP BY player_id, game_name
                """,
                {"rs": system, "ids": tuple(player_ids)},
            )
            return {(r[0], r[1]): (round(r[2], 1), r[3], round(r[4], 1)) for r in peak_rows}
        peak_rows = self.db.client.execute(
            """
            SELECT player_id, game_name,
                   max(rating) AS peak,
                   argMax(played_at, rating) AS peak_date
            FROM rating_history
            WHERE rating_system = %(rs)s AND player_id IN %(ids)s
            GROUP BY player_id, game_name
            """,
            {"rs": system, "ids": tuple(player_ids)},
        )
        return {(r[0], r[1]): (round(r[2], 1), r[3], None) for r in peak_rows}

    def get_top_players(
        self,
        game: str = "",
        system: str = "elo",
        limit: int = 20,
        min_matches: int = 0,
        sort_by: str = "rating",
        sort_col: str = "",
        sort_dir: str = "desc",
        offset: int = 0,
        fetch_peaks: bool = True,
    ) -> list[dict]:
        """Top players by rating or peak.

        Args:
            game: game name ("" = all games)
            system: "elo" or "glicko2"
            limit: max results
            min_matches: minimum matches played to qualify
            sort_by: "rating" (current rating) or "peak" (all-time peak)
            sort_col: optional column to sort by (server-side, ALL data).
                When set, fetches all qualifying players and sorts in Python.
            sort_dir: "asc" or "desc" (used with sort_col).
            offset: row offset for pagination.
            fetch_peaks: whether to fetch per-player peak rating/date from
                rating_history. Callers that don't display peaks (e.g. the
                home page top-players table, or leaderboard sorted by rating)
                can set False to skip an extra query.
        """
        if sort_by == "peak" and not sort_col:
            return self._get_top_players_by_peak(
                game=game, system=system, limit=limit, min_matches=min_matches, offset=offset
            )

        # When sorting by an arbitrary column, fetch ALL qualifying players
        # (no LIMIT) so the sort applies to the full dataset, then slice.
        # Otherwise, fetch offset+limit rows and slice in Python (rank = offset+i+1).
        fetch_limit = 100000 if sort_col else (offset + limit)

        params = {"sys": system, "game": game, "lim": fetch_limit}
        if min_matches > 0:
            params["mm"] = min_matches

        if not game:
            # All Games: fetch main game per player via separate query.
            # No FINAL: the All Games aggregate rows have zero duplicates for
            # both elo and glicko2, so FINAL is a no-op (and costs a sort).
            query = """
                SELECT player_id, player_name, rating, rd, vol, wins, losses, matches_played,
                       last_match_date, first_match_date
                FROM player_ratings
                WHERE rating_system = %(sys)s AND game_name = ''
            """
            if min_matches > 0:
                query += " AND matches_played >= %(mm)s"
            # Glicko-2: sort by rating - RD (conservative lower bound) to demote uncertain players
            if system == "glicko2":
                query += f" ORDER BY {_glicko_rank_sql()} DESC LIMIT %(lim)s"
            else:
                query += " ORDER BY rating DESC LIMIT %(lim)s"
            rows = self.db.client.execute(query, params)

            # Batch-fetch main game (most matches) for all player IDs
            player_ids = [r[0] for r in rows]
            canon = self._canonical_names(player_ids)
            countries = self._countries(player_ids)
            main_games = {}
            peaks = {}
            if player_ids:
                # Per-game elo rows have zero duplicates (FINAL is a no-op and
                # costs a sort); glicko2 per-game rows have duplicates, so keep
                # FINAL only for glicko2. Cache per system — main_game only
                # changes on import.
                mg_cache = _MAIN_GAMES_CACHE.get(system)
                if mg_cache is not None:
                    main_games = {pid: mg_cache[pid] for pid in player_ids if pid in mg_cache}
                    missing_mg = [pid for pid in player_ids if pid not in mg_cache]
                else:
                    main_games = {}
                    missing_mg = list(player_ids)
                if missing_mg:
                    mg_final = " FINAL" if system == "glicko2" else ""
                    mg_rows = self.db.client.execute(
                        f"SELECT player_id, argMax(game_name, matches_played) AS main_game "
                        f"FROM player_ratings{mg_final} "
                        f"WHERE rating_system = %(sys)s AND game_name != '' AND player_id IN %(ids)s "
                        f"GROUP BY player_id",
                        {"sys": system, "ids": tuple(missing_mg)},
                    )
                    if mg_cache is None:
                        mg_cache = {}
                        _MAIN_GAMES_CACHE[system] = mg_cache
                    for r in mg_rows:
                        main_games[r[0]] = r[1]
                        mg_cache[r[0]] = r[1]
                if fetch_peaks:
                    peaks = self._fetch_peaks(player_ids, system)

            results = [
                {
                    "player_id": r[0],
                    "name": canon.get(r[0], r[1]),
                    "country": countries.get(r[0], ""),
                    "rating": round(r[2], 1),
                    "rd": round(r[3], 1) if r[3] else None,
                    "vol": round(r[4], 4) if r[4] else None,
                    "wins": r[5],
                    "losses": r[6],
                    "matches": r[7],
                    "last_match_date": r[8],
                    "first_match_date": r[9],
                    "main_game": main_games.get(r[0], ""),
                    "peak": peaks.get((r[0], ""), (None, None))[0],
                    "peak_date": peaks.get((r[0], ""), (None, None))[1],
                    "rank": i + 1,
                }
                for i, r in enumerate(rows)
            ]
            if sort_col:
                results = _sort_players(results, sort_col, sort_dir)
            return results[offset:offset + limit]
        else:
            # Per-game: elo rows have zero duplicates (FINAL is a no-op), but
            # glicko2 rows have duplicates, so keep FINAL only for glicko2.
            final_clause = " FINAL" if system == "glicko2" else ""
            query = f"""
                SELECT player_id, player_name, rating, rd, vol, wins, losses, matches_played,
                       last_match_date, first_match_date
                FROM player_ratings{final_clause}
                WHERE rating_system = %(sys)s AND game_name = %(game)s
            """
            if min_matches > 0:
                query += " AND matches_played >= %(mm)s"
            # Glicko-2: sort by rating - RD (conservative lower bound)
            if system == "glicko2":
                query += f" ORDER BY {_glicko_rank_sql()} DESC LIMIT %(lim)s"
            else:
                query += " ORDER BY rating DESC LIMIT %(lim)s"
            rows = self.db.client.execute(query, params)
            player_ids = [r[0] for r in rows]
            canon = self._canonical_names(player_ids)
            countries = self._countries(player_ids)
            peaks = self._fetch_peaks(player_ids, system) if fetch_peaks else {}
            results = [
                {
                    "player_id": r[0],
                    "name": canon.get(r[0], r[1]),
                    "country": countries.get(r[0], ""),
                    "rating": round(r[2], 1),
                    "rd": round(r[3], 1) if r[3] else None,
                    "vol": round(r[4], 4) if r[4] else None,
                    "wins": r[5],
                    "losses": r[6],
                    "matches": r[7],
                    "last_match_date": r[8],
                    "first_match_date": r[9],
                    "main_game": "",
                    "peak": peaks.get((r[0], game), (None, None, None))[0],
                    "peak_date": peaks.get((r[0], game), (None, None, None))[1],
                    "rd_at_peak": peaks.get((r[0], game), (None, None, None))[2],
                    "rank": i + 1,
                }
                for i, r in enumerate(rows)
            ]
            if sort_col:
                results = _sort_players(results, sort_col, sort_dir)
            return results[offset:offset + limit]

    def get_ratings_for_players(
        self, player_ids: list, system: str = "glicko2", game: str = ""
    ) -> dict:
        """Batch-fetch a rating (default glicko2, all games) for a set of player IDs.

        Returns {player_id: rating_value}.
        """
        if not player_ids:
            return {}
        rows = self.db.client.execute(
            """
            SELECT player_id, rating
            FROM player_ratings
            WHERE rating_system = %(sys)s AND game_name = %(game)s
              AND player_id IN %(ids)s
            """,
            {"sys": system, "game": game, "ids": tuple(player_ids)},
        )
        return {r[0]: round(r[1], 1) for r in rows}

    def _get_top_players_by_peak(
        self,
        game: str = "",
        system: str = "elo",
        limit: int = 20,
        min_matches: int = 0,
        offset: int = 0,
    ) -> list[dict]:
        """Top players by all-time peak rating (from rating_history)."""
        params = {"rs": system, "game": game, "lim": offset + limit}
        if min_matches > 0:
            params["mm"] = min_matches

        game_filter = "game_name = %(game)s" if game else "game_name = ''"

        # Step 1: Get top player_ids by peak from rating_history
        # For Glicko-2: sort by peak - rd_at_peak (conservative) to match rating sort behavior
        if system == "glicko2":
            peak_query = f"""
                SELECT player_id,
                       max(rating) AS peak,
                       argMax(played_at, rating) AS peak_date,
                       argMax(rd, rating) AS rd_at_peak
                FROM rating_history
                WHERE rating_system = %(rs)s AND {game_filter}
                GROUP BY player_id
            """
            if min_matches > 0:
                peak_query = f"""
                    SELECT rh.player_id,
                           max(rh.rating) AS peak,
                           argMax(rh.played_at, rh.rating) AS peak_date,
                           argMax(rh.rd, rh.rating) AS rd_at_peak
                    FROM rating_history rh
                    WHERE rh.rating_system = %(rs)s AND rh.{game_filter} AND rh.matches_played >= %(mm)s
                    GROUP BY rh.player_id
                """
            peak_query += " ORDER BY peak - rd_at_peak DESC LIMIT %(lim)s"
        else:
            peak_query = f"""
                SELECT player_id,
                       max(rating) AS peak,
                       argMax(played_at, rating) AS peak_date
                FROM rating_history
                WHERE rating_system = %(rs)s AND {game_filter}
                GROUP BY player_id
            """
            if min_matches > 0:
                peak_query = f"""
                    SELECT rh.player_id,
                           max(rh.rating) AS peak,
                           argMax(rh.played_at, rh.rating) AS peak_date
                    FROM rating_history rh
                    WHERE rh.rating_system = %(rs)s AND rh.{game_filter} AND rh.matches_played >= %(mm)s
                    GROUP BY rh.player_id
                """
            peak_query += " ORDER BY peak DESC LIMIT %(lim)s"
        peak_rows = self.db.client.execute(peak_query, params)
        if not peak_rows:
            return []

        player_ids = [r[0] for r in peak_rows]
        # For glicko2 the displayed peak is the conservative lower bound
        # (peak - rd_at_peak), matching the sort and the home page plaque.
        # rd_at_peak is kept so the RD column can show the RD at the peak date.
        if system == "glicko2":
            peak_map = {r[0]: (round(r[1] - r[3], 1), r[2], round(r[3], 1)) for r in peak_rows}
        else:
            peak_map = {r[0]: (round(r[1], 1), r[2], None) for r in peak_rows}
        countries = self._countries(player_ids)
        # Pre-fetch canonical names ONCE (batched) — calling _canonical_name per
        # player in the merge loop below would issue one query per player (N+1).
        canon_names = self._canonical_names(player_ids)

        # Step 2: Fetch current ratings from player_ratings for those player_ids
        rating_query = """
            SELECT player_id, player_name, rating, rd, vol, wins, losses, matches_played,
                   last_match_date, first_match_date
            FROM player_ratings FINAL
            WHERE rating_system = %(sys)s AND {game_filter_2} AND player_id IN %(ids)s
        """
        game_filter_2 = "game_name = %(game)s" if game else "game_name = ''"
        rating_query = rating_query.format(game_filter_2=game_filter_2)
        rating_params = {"sys": system, "game": game, "ids": tuple(player_ids)}
        if min_matches > 0:
            rating_query += " AND matches_played >= %(mm)s"
            rating_params["mm"] = min_matches
        rating_rows = self.db.client.execute(rating_query, rating_params)
        rating_map = {r[0]: r for r in rating_rows}

        # Step 3: Fetch main game for combined view
        main_games = {}
        if not game and player_ids:
            mg_rows = self.db.client.execute(
                "SELECT player_id, argMax(game_name, matches_played) AS main_game "
                "FROM player_ratings FINAL "
                "WHERE rating_system = %(sys)s AND game_name != '' AND player_id IN %(ids)s "
                "GROUP BY player_id",
                {"sys": system, "ids": tuple(player_ids)},
            )
            main_games = {r[0]: r[1] for r in mg_rows}

        # Step 4: Merge — order by peak (from step 1), fill in current rating data
        results = []
        for rank, pid in enumerate(player_ids, start=1):
            r = rating_map.get(pid)
            pk, pkd, rd_at_peak = peak_map[pid]
            if r:
                results.append({
                    "player_id": r[0],
                    "name": canon_names.get(r[0], self._canonical_name(r[0])),
                    "country": countries.get(r[0], ""),
                    "rating": round(r[2], 1),
                    "rd": round(r[3], 1) if r[3] else None,
                    "vol": round(r[4], 4) if r[4] else None,
                    "wins": r[5],
                    "losses": r[6],
                    "matches": r[7],
                    "last_match_date": r[8],
                    "first_match_date": r[9],
                    "main_game": main_games.get(r[0], ""),
                    "peak": pk,
                    "peak_date": pkd,
                    "rd_at_peak": rd_at_peak,
                    "rank": rank,
                })
            else:
                # Player has history but no current rating (e.g. retired)
                name = canon_names.get(pid, self._canonical_name(pid))
                results.append({
                    "player_id": pid,
                    "name": name,
                    "country": countries.get(pid, ""),
                    "rating": None,
                    "rd": None,
                    "vol": None,
                    "wins": None,
                    "losses": None,
                    "matches": None,
                    "last_match_date": None,
                    "first_match_date": None,
                    "main_game": main_games.get(pid, ""),
                    "peak": pk,
                    "peak_date": pkd,
                    "rd_at_peak": rd_at_peak,
                    "rank": rank,
                })
        return results[offset:offset + limit]

    def get_top_players_asof(
        self,
        date: str,
        game: str = "",
        system: str = "elo",
        limit: int = 20,
        min_matches: int = 0,
        sort_by: str = "rating",
        sort_col: str = "",
        sort_dir: str = "desc",
        offset: int = 0,
    ) -> list[dict]:
        """Top players by rating as of a specific date.

        Uses rating_history snapshots to reconstruct the leaderboard at a point in time.
        For each player, takes their latest rating snapshot at or before the given date.

        Args:
            date: ISO date string (e.g. '2020-01-01').
            game: game name ("" = all games).
            system: "elo" or "glicko2".
            limit: max results.
            min_matches: minimum matches played to qualify.
            sort_by: "rating" or "peak".
            sort_col: optional column to sort by (server-side, ALL data).
                When set, fetches all qualifying players and sorts in Python.
            sort_dir: "asc" or "desc" (used with sort_col).
        """
        # When sorting by an arbitrary column, fetch ALL qualifying players
        # (no LIMIT) so the sort applies to the full dataset, then slice.
        # Otherwise, fetch offset+limit rows and slice in Python (rank = offset+i+1).
        fetch_limit = 100000 if sort_col else (offset + limit)
        params = {"rs": system, "game": game, "lim": fetch_limit, "date": f"{date} 00:00:00"}
        if min_matches > 0:
            params["mm"] = min_matches

        if not game:
            game_filter = "game_name = ''"
        else:
            game_filter = "game_name = %(game)s"

        # Determine sort column
        if sort_by == "peak":
            # Sort by peak rating (max rating in history up to date) instead of as-of rating
            # Use a subquery to compute peak separately to avoid nested aggregate error
            # For Glicko-2: sort by peak - rd_at_peak (conservative) to match rating sort behavior
            if system == "glicko2":
                peak_subquery = f"""
                    SELECT player_id, max(rating) AS peak, argMax(played_at, rating) AS peak_date,
                           argMax(rd, rating) AS rd_at_peak
                    FROM rating_history
                    WHERE rating_system = %(rs)s AND {game_filter} AND played_at <= toDateTime(%(date)s)
                    GROUP BY player_id
                """
                order_expr = "p.peak - p.rd_at_peak DESC"
                group_extra = ", p.rd_at_peak"
            else:
                peak_subquery = f"""
                    SELECT player_id, max(rating) AS peak, argMax(played_at, rating) AS peak_date
                    FROM rating_history
                    WHERE rating_system = %(rs)s AND {game_filter} AND played_at <= toDateTime(%(date)s)
                    GROUP BY player_id
                """
                order_expr = "p.peak DESC"
                group_extra = ""
            # For glicko2 the displayed peak is the conservative lower bound
            # (peak - rd_at_peak), matching the sort and home page plaque.
            peak_expr = "p.peak - p.rd_at_peak" if system == "glicko2" else "p.peak"
            rd_at_peak_expr = ", p.rd_at_peak" if system == "glicko2" else ""
            query = f"""
                SELECT h.player_id,
                       argMax(h.rating, h.played_at) AS rating,
                       argMax(h.rd, h.played_at) AS rd,
                       argMax(h.vol, h.played_at) AS vol,
                       argMax(h.wins, h.played_at) AS wins,
                       argMax(h.losses, h.played_at) AS losses,
                       argMax(h.matches_played, h.played_at) AS matches,
                       max(h.played_at) AS last_match,
                       {peak_expr} AS peak,
                       p.peak_date{rd_at_peak_expr}
                FROM rating_history h
                INNER JOIN (
                    {peak_subquery}
                ) p ON h.player_id = p.player_id
                WHERE h.rating_system = %(rs)s AND h.{game_filter} AND h.played_at <= toDateTime(%(date)s)
                GROUP BY h.player_id, p.peak, p.peak_date{group_extra}
            """
        else:
            order_expr = _glicko_order_expr(system)
            query = f"""
                SELECT player_id,
                       argMax(rating, played_at) AS rating,
                       argMax(rd, played_at) AS rd,
                       argMax(vol, played_at) AS vol,
                       argMax(wins, played_at) AS wins,
                       argMax(losses, played_at) AS losses,
                       argMax(matches_played, played_at) AS matches,
                       max(played_at) AS last_match
                FROM rating_history
                WHERE rating_system = %(rs)s AND {game_filter} AND played_at <= toDateTime(%(date)s)
                GROUP BY player_id
            """
        if min_matches > 0:
            query += " HAVING matches >= %(mm)s"
        query += f" ORDER BY {order_expr} LIMIT %(lim)s"

        rows = self.db.client.execute(query, params)

        # Fetch player names and first_match_date from player_ratings (rating_history doesn't store these)
        player_ids = [r[0] for r in rows]
        names = {}
        first_dates = {}
        peaks = {}
        countries = self._countries(player_ids)
        if player_ids:
            nr_rows = self.db.client.execute(
                "SELECT player_id, argMax(player_name, last_match_date) AS name, "
                "min(first_match_date) AS first_date "
                "FROM player_ratings FINAL WHERE player_id IN %(ids)s GROUP BY player_id",
                {"ids": tuple(player_ids)},
            )
            for r in nr_rows:
                names[r[0]] = r[1]
                first_dates[r[0]] = r[2]
            # Peak as of the given date — only consider history up to that date.
            # For glicko2 the displayed peak is the conservative lower bound
            # (peak - rd_at_peak), matching the sort and home page plaque.
            if system == "glicko2":
                peak_rows = self.db.client.execute(
                    """
                    SELECT player_id, game_name,
                           max(rating) - argMax(rd, rating) AS peak,
                           argMax(played_at, rating) AS peak_date,
                           argMax(rd, rating) AS rd_at_peak
                    FROM rating_history
                    WHERE rating_system = %(rs)s AND player_id IN %(ids)s AND played_at <= toDateTime(%(date)s)
                    GROUP BY player_id, game_name
                    """,
                    {"rs": system, "ids": tuple(player_ids), "date": f"{date} 00:00:00"},
                )
                peaks = {(r[0], r[1]): (round(r[2], 1), r[3], round(r[4], 1)) for r in peak_rows}
            else:
                peak_rows = self.db.client.execute(
                    """
                    SELECT player_id, game_name,
                           max(rating) AS peak,
                           argMax(played_at, rating) AS peak_date
                    FROM rating_history
                    WHERE rating_system = %(rs)s AND player_id IN %(ids)s AND played_at <= toDateTime(%(date)s)
                    GROUP BY player_id, game_name
                    """,
                    {"rs": system, "ids": tuple(player_ids), "date": f"{date} 00:00:00"},
                )
                peaks = {(r[0], r[1]): (round(r[2], 1), r[3], None) for r in peak_rows}

        # For combined: also get main game per player
        main_games = {}
        if not game and player_ids:
            mg_rows = self.db.client.execute(
                "SELECT player_id, argMax(game_name, matches_played) AS main_game "
                "FROM player_ratings FINAL WHERE rating_system = %(rs)s AND game_name != '' AND player_id IN %(ids)s GROUP BY player_id",
                {"rs": system, "ids": tuple(player_ids)},
            )
            main_games = {r[0]: r[1] for r in mg_rows}

        results = [
            {
                "player_id": r[0],
                "name": names.get(r[0], f"player_{r[0]}"),
                "country": countries.get(r[0], ""),
                "rating": round(r[1], 1),
                "rd": round(r[2], 1) if r[2] else None,
                "vol": round(r[3], 4) if r[3] else None,
                "wins": r[4],
                "losses": r[5],
                "matches": r[6],
                "last_match_date": r[7],
                "first_match_date": first_dates.get(r[0]),
                "main_game": main_games.get(r[0], ""),
                "peak": peaks.get((r[0], game), (None, None, None))[0],
                "peak_date": peaks.get((r[0], game), (None, None, None))[1],
                "rd_at_peak": peaks.get((r[0], game), (None, None, None))[2],
                "rank": i + 1,
            }
            for i, r in enumerate(rows)
        ]
        if sort_col:
            results = _sort_players(results, sort_col, sort_dir)
        return results[offset:offset + limit]

    def count_top_players(
        self,
        game: str = "",
        system: str = "elo",
        min_matches: int = 0,
    ) -> int:
        """Total number of qualifying players (for pagination)."""
        params = {"sys": system, "game": game}
        conds = ["rating_system = %(sys)s"]
        if game:
            conds.append("game_name = %(game)s")
        else:
            conds.append("game_name = ''")
        if min_matches > 0:
            params["mm"] = min_matches
            conds.append("matches_played >= %(mm)s")
        query = (
            "SELECT count() FROM player_ratings FINAL WHERE " + " AND ".join(conds)
        )
        rows = self.db.client.execute(query, params)
        return rows[0][0] if rows else 0

    def count_top_players_asof(
        self,
        date: str,
        game: str = "",
        system: str = "elo",
        min_matches: int = 0,
    ) -> int:
        """Total number of qualifying players as of a date (for pagination)."""
        params = {"rs": system, "game": game, "date": f"{date} 00:00:00"}
        if game:
            game_filter = "game_name = %(game)s"
        else:
            game_filter = "game_name = ''"
        query = f"""
            SELECT count()
            FROM (
                SELECT player_id,
                       argMax(matches_played, played_at) AS matches
                FROM rating_history
                WHERE rating_system = %(rs)s AND {game_filter} AND played_at <= toDateTime(%(date)s)
                GROUP BY player_id
            )
        """
        if min_matches > 0:
            params["mm"] = min_matches
            query = f"""
                SELECT count()
                FROM (
                    SELECT player_id,
                           argMax(matches_played, played_at) AS matches
                    FROM rating_history
                    WHERE rating_system = %(rs)s AND {game_filter} AND played_at <= toDateTime(%(date)s)
                    GROUP BY player_id
                    HAVING matches >= %(mm)s
                )
            """
        rows = self.db.client.execute(query, params)
        return rows[0][0] if rows else 0

    # --- Player lookup ---

    def get_player_ratings(self, player_name: str, min_matches: dict | int = 0) -> list[dict]:
        """All ratings for a player across games and systems.

        min_matches: either an int (applied to all systems) or a dict mapping
        system name -> threshold. If a threshold > 0, the displayed peak is the
        highest peak reached at a moment when the player had at least that many
        matches (matching the leaderboard and home page peak semantics).
        """
        player_id = self._player_id(player_name)
        if player_id is None:
            return []
        canon = self._canonical_name(player_id)
        rows = self.db.client.execute(
            """
            SELECT player_id, player_name, game_name, rating_system, rating, rd, vol,
                   wins, losses, matches_played, last_match_id, last_match_date, first_match_date
            FROM player_ratings FINAL
            WHERE player_id = %(pid)s
            ORDER BY rating_system, rating DESC
            """,
            {"pid": player_id},
        )

        player_id = rows[0][0] if rows else None
        peaks = {}
        if player_id:
            # For glicko2 the displayed peak is the conservative lower bound
            # (peak - rd_at_peak), matching the leaderboard and home page plaque.
            mm_g = min_matches.get("glicko2", 0) if isinstance(min_matches, dict) else min_matches
            mm_other = min_matches.get("elo", 0) if isinstance(min_matches, dict) else min_matches
            # One combined query for both systems. Glicko-2 applies its own
            # min_matches threshold; other systems use mm_other. rd_at_peak is
            # only meaningful for glicko2 (set to None for other systems below).
            peak_rows = self.db.client.execute(
                f"""
                SELECT rating_system, game_name,
                       max(rating) - if(rating_system = 'glicko2', argMax(rd, rating), 0) AS peak,
                       argMax(played_at, rating) AS peak_date,
                       argMax(rd, rating) AS rd_at_peak
                FROM rating_history
                WHERE player_id = %(pid)s
                  AND (rating_system = 'glicko2' AND matches_played >= %(mm_g)s
                       OR rating_system != 'glicko2' AND matches_played >= %(mm_o)s)
                GROUP BY rating_system, game_name
                """,
                {"pid": player_id, "mm_g": mm_g, "mm_o": mm_other},
            )
            for r in peak_rows:
                rd_at_peak = round(r[4], 1) if r[0] == "glicko2" else None
                peaks[(r[0], r[1])] = (round(r[2], 1), r[3], rd_at_peak)

        return [
            {
                "player_id": r[0],
                "name": canon,
                "country": self._country(r[0]),
                "game": r[2] or "All Games",
                "system": r[3],
                "rating": round(r[4], 1),
                "_raw_rating": r[4],
                "rd": round(r[5], 1) if r[5] else None,
                "_raw_rd": r[5],
                "vol": round(r[6], 4) if r[6] else None,
                "wins": r[7],
                "losses": r[8],
                "matches": r[9],
                "last_match_id": r[10],
                "last_match_date": r[11],
                "first_match_date": r[12],
                "peak": peaks.get((r[3], r[2]), (None, None, None))[0],
                "peak_date": peaks.get((r[3], r[2]), (None, None, None))[1],
                "rd_at_peak": peaks.get((r[3], r[2]), (None, None, None))[2],
            }
            for r in rows
        ]

    def get_player_history(
        self,
        player_name: str,
        system: str = "elo",
        game: str = "",
        limit: int = 50,
    ) -> list[dict]:
        """Rating history for a player (most recent first)."""
        player_id = self._player_id(player_name)
        if player_id is None:
            return []
        rows = self.db.client.execute(
            """
            SELECT match_id, played_at, rating, rd, vol, wins, losses, matches_played
            FROM rating_history
            WHERE player_id = %(pid)s
            AND rating_system = %(sys)s AND game_name = %(game)s
            ORDER BY played_at DESC, match_id DESC
            LIMIT %(lim)s
            """,
            {"pid": player_id, "game": game, "sys": system, "lim": limit},
        )
        return [
            {
                "match_id": r[0],
                "played_at": r[1],
                "rating": round(r[2], 1),
                "rd": round(r[3], 1) if r[3] else None,
                "vol": round(r[4], 4) if r[4] else None,
                "wins": r[5],
                "losses": r[6],
                "matches": r[7],
            }
            for r in rows
        ]

    def get_player_history_both(
        self,
        player_name: str,
        game: str = "",
        limit: int = 50,
        since: str = "",
    ) -> list[dict]:
        """Rating history for a player with both ELO and Glicko-2 per row.

        Uses ASOF join on the configured Glicko-2 period function so Glicko-2 ratings
        forward-fill onto every ELO match row within the same period.
        """
        player_id = self._player_id(player_name)
        if player_id is None:
            return []
        ch_fn = _glicko_period_ch_fn()
        since_clause = ""
        params: dict = {"pid": player_id, "game": game, "lim": limit}
        if since:
            since_clause = "AND played_at >= %(since)s"
            params["since"] = since
        # Fetch the player's ELO rows and Glicko-2 rows separately, then
        # forward-fill Glicko-2 onto each ELO row in Python by the configured
        # period. This replaces the previous ASOF self-join (which scanned the
        # player's full rating_history on both sides and was ~18ms regardless
        # of limit) with two index-friendly queries + a fast in-memory merge.
        elo_rows = self.db.client.execute(
            f"""
            SELECT match_id, played_at, rating, wins, losses, matches_played
            FROM rating_history
            WHERE player_id = %(pid)s AND rating_system = 'elo'
              AND game_name = %(game)s {since_clause}
            ORDER BY played_at, match_id
            """,
            params,
        )
        glicko_rows = self.db.client.execute(
            """
            SELECT played_at, rating, rd, vol
            FROM rating_history
            WHERE player_id = %(pid)s AND rating_system = 'glicko2'
              AND game_name = %(game)s
            ORDER BY played_at, match_id
            """,
            {"pid": player_id, "game": game},
        )
        # Period key matching the configured Glicko-2 period function.
        def _period_key(ts):
            if ch_fn == "toStartOfYear":
                return (ts.year,)
            if ch_fn == "toStartOfMonth":
                return (ts.year, ts.month)
            if ch_fn == "toStartOfWeek":
                iso = ts.isocalendar()
                return (iso.year, iso.week)
            return (ts.year, ts.month, ts.day)

        g_keys = [_period_key(r[0]) for r in glicko_rows]
        rows = []
        for mid, ts, rating, wins, losses, mp in elo_rows:
            idx = bisect.bisect_right(g_keys, _period_key(ts)) - 1
            if idx >= 0:
                _, gr, grd, gvol = glicko_rows[idx]
                rows.append((mid, ts, rating, gr, grd, gvol, wins, losses, mp))
            else:
                rows.append((mid, ts, rating, None, None, None, wins, losses, mp))
        # Most recent first, then limit (matches the previous ORDER BY + LIMIT).
        rows.sort(key=lambda r: (r[1], r[0]), reverse=True)
        rows = rows[:limit]
        return [
            {
                "match_id": r[0],
                "played_at": r[1],
                "elo": round(r[2], 1) if r[2] else None,
                "glicko2": round(r[3], 1) if r[3] else None,
                "rd": round(r[4], 1) if r[4] else None,
                "vol": round(r[5], 4) if r[5] else None,
                "wins": r[6],
                "losses": r[7],
                "matches": r[8],
            }
            for r in rows
        ]

    # --- Head-to-head ---

    def get_head_to_head(
        self,
        player1: str,
        player2: str,
        game: str = "",
        limit: int = 100000,
        match: str = "exact",
        match2: Optional[str] = None,
    ) -> dict:
        """Head-to-head record between two players.

        The scoreboard (p1_wins/p2_wins/total) reflects the FULL head-to-head
        record, and the returned match list includes ALL matches (no practical
        limit). Winner determination uses the authoritative
        `winner_id` (a player_id) rather than score comparison, which also
        handles draws correctly. Each match is normalized so that
        player1/player2 always correspond to the requested order.

        `match`/`match2` control how player1/player2 match names (each may
        differ, e.g. for smart auto-detection):
          - 'exact'   : case-insensitive exact name match (default)
          - 'partial' : case-insensitive substring match
          - 'regex'   : case-insensitive regular expression match
        """
        match2 = match2 or match

        def _cond(col, mode, pat, ph):
            if mode == "regex":
                return f"match(lowerUTF8({col}), lowerUTF8(%({ph})s))", pat
            if mode == "partial":
                return f"{col} ILIKE %({ph})s", f"%{pat}%"
            return f"{col} = %({ph})s", pat

        # Build per-player conditions. For exact mode resolve to player_id so
        # the match is robust to name-spelling variations (canonical name).
        p1_id = self._player_id(player1) if match == "exact" else None
        p2_id = self._player_id(player2) if match2 == "exact" else None
        if match == "exact" and match2 == "exact" and p1_id is not None and p2_id is not None:
            # Both resolved to ids: match by player_id (handles any spelling).
            where = (
                f"(m.player1_id = %(p1id)s AND m.player2_id = %(p2id)s)"
                f" OR (m.player1_id = %(p2id)s AND m.player2_id = %(p1id)s)"
            )
            params = {"p1id": p1_id, "p2id": p2_id}
            if game:
                where = f"m.game_name = %(game)s AND ({where})"
                params["game"] = game
        else:
            # Fall back to name-based matching (partial/regex, or unresolved id).
            if match == "exact":
                player1 = self._resolve_name(player1)
            if match2 == "exact":
                player2 = self._resolve_name(player2)
            p1_cond, p1 = _cond("m.player1_name", match, player1, "p1")
            p2_cond, p2 = _cond("m.player2_name", match2, player2, "p2")
            p1_cond_r, _ = _cond("m.player1_name", match2, player2, "p2")
            p2_cond_r, _ = _cond("m.player2_name", match, player1, "p1")
            where = (
                f"({p1_cond} AND {p2_cond})"
                f" OR ({p1_cond_r} AND {p2_cond_r})"
            )
            params = {"p1": p1, "p2": p2}
            if game:
                where = f"m.game_name = %(game)s AND ({where})"
                params["game"] = game

        # For partial/regex modes a single player_id can't be resolved, so wins
        # are counted by name-pattern matching instead.

        # Full-record counts (not truncated by LIMIT).
        if match == "exact" and match2 == "exact":
            count_row = self.db.client.execute(
                f"""
                SELECT
                    countIf(m.winner_id = %(p1id)s),
                    countIf(m.winner_id = %(p2id)s),
                    count()
                FROM matches m
                WHERE {where}
                """,
                {**params, "p1id": p1_id, "p2id": p2_id},
            )
        else:
            # Count wins by name pattern: a match is a p1-win if the winner's
            # name matches the p1 pattern (and the loser matches p2), etc.
            if match == "regex":
                w1 = "match(lowerUTF8(winner_name), lowerUTF8(%(p1)s))"
            elif match == "partial":
                w1 = "winner_name ILIKE %(p1)s"
            else:
                w1 = "winner_name = %(p1)s"
            if match2 == "regex":
                w2 = "match(lowerUTF8(winner_name), lowerUTF8(%(p2)s))"
            elif match2 == "partial":
                w2 = "winner_name ILIKE %(p2)s"
            else:
                w2 = "winner_name = %(p2)s"
            count_row = self.db.client.execute(
                f"""
                SELECT
                    countIf({w1}),
                    countIf({w2}),
                    count()
                FROM (
                    SELECT
                        m.match_id,
                        CASE
                            WHEN m.winner_id = m.player1_id THEN m.player1_name
                            WHEN m.winner_id = m.player2_id THEN m.player2_name
                            ELSE NULL
                        END AS winner_name
                    FROM matches m
                    WHERE {where}
                )
                """,
                params,
            )
        p1_wins = count_row[0][0] if count_row else 0
        p2_wins = count_row[0][1] if count_row else 0
        total = count_row[0][2] if count_row else 0

        # Most recent matches, limited for display.
        rows = self.db.client.execute(
            f"""
            SELECT m.match_id, m.player1_name, m.player2_name, m.player1_score, m.player2_score,
                   m.player1_id, m.player2_id, m.winner_id, m.game_name, m.tournament_name,
                   m.stage_name, m.played_at, t.tier, m.tournament_id
            FROM matches m
            LEFT JOIN tournaments t ON m.tournament_id = t.tournament_id
            WHERE {where}
            ORDER BY m.played_at DESC
            LIMIT %(lim)s
            """,
            {**params, "lim": limit},
        )
        matches = []
        for r in rows:
            (mid, p1n, p2n, s1, s2, p1id, p2id, wid, gn, tn, st, pa, tier, tid) = r
            # Authoritative winner from winner_id (a player_id).
            if wid is not None and wid == p1id:
                w = p1n
            elif wid is not None and wid == p2id:
                w = p2n
            else:
                w = None  # draw / unknown
            # Normalize to requested order: player1/player2 must match the
            # requested player1/player2, swapping stored names/scores as needed.
            # For exact mode compare by player_id; for partial/regex compare
            # against the raw patterns.
            if match == "exact":
                p1_matches = (p1id == p1_id)
            elif match == "regex":
                import re
                p1_matches = bool(re.search(player1, p1n, re.IGNORECASE))
            else:
                p1_matches = (player1.lower() in p1n.lower())
            if p1_matches:
                disp1, disp2, ds1, ds2 = p1n, p2n, s1, s2
            else:
                disp1, disp2, ds1, ds2 = p2n, p1n, s2, s1
            # Map display names to canonical (most-used) spelling per player_id.
            canon = self._canonical_names([p1id, p2id])
            disp1 = canon.get(p1id if p1_matches else p2id, disp1)
            disp2 = canon.get(p2id if p1_matches else p1id, disp2)
            matches.append({
                "match_id": mid,
                "player1": disp1,
                "player2": disp2,
                "player1_id": p1id if p1_matches else p2id,
                "player2_id": p2id if p1_matches else p1id,
                "player1_country": self._country(p1id if p1_matches else p2id),
                "player2_country": self._country(p2id if p1_matches else p1id),
                "score": f"{ds1}-{ds2}",
                "score1": ds1,
                "score2": ds2,
                "winner": w,
                "game": gn,
                "tournament": tn,
                "tournament_id": tid,
                "stage": st,
                "played_at": pa,
                "tier": tier,
            })
        return {
            "player1": player1,
            "player2": player2,
            "p1_id": p1_id,
            "p2_id": p2_id,
            "p1_country": self._country(p1_id) if p1_id else "",
            "p2_country": self._country(p2_id) if p2_id else "",
            "p1_wins": p1_wins,
            "p2_wins": p2_wins,
            "total": total,
            "matches": matches,
        }

    # --- Recent matches ---

    def get_recent_matches(
        self,
        game: str = "",
        limit: int = 20,
        player: str = "",
        tournament: str = "",
        tier: str = "",
        offset: int = 0,
        sort_col: str = "",
        sort_dir: str = "desc",
        match: str = "exact",
        player_match: Optional[str] = None,
        tournament_match: Optional[str] = None,
    ) -> list[dict]:
        """Recent matches (newest first).

        Optional filters: game, player (name), tournament (name), tier.
        `match` controls how player/tournament filters match:
          - 'exact'   : case-insensitive exact name match (default)
          - 'partial' : case-insensitive substring match
          - 'regex'   : case-insensitive regular expression match
        `player_match`/`tournament_match` override `match` per-field (used for
        smart auto-detection where player and tournament may need different
        modes). When sort_col is set, ALL matching rows are fetched and sorted
        in Python so the sort applies to the full dataset, not just the page.
        """
        player_match = player_match or match
        tournament_match = tournament_match or match
        # The tier filter needs the tournaments JOIN; when no tier filter is
        # present we drop the JOIN (it roughly doubles the query cost on the
        # full matches sort) and fetch tiers for the returned rows separately.
        need_join = bool(tier)
        if need_join:
            query = """
                SELECT m.match_id, m.player1_name, m.player2_name, m.player1_score, m.player2_score,
                       m.player1_id, m.player2_id, m.winner_id, m.game_name, m.tournament_name, m.stage_name, m.played_at, t.tier,
                       m.tournament_id
                FROM matches m
                LEFT JOIN tournaments t ON m.tournament_id = t.tournament_id
            """
        else:
            query = """
                SELECT m.match_id, m.player1_name, m.player2_name, m.player1_score, m.player2_score,
                       m.player1_id, m.player2_id, m.winner_id, m.game_name, m.tournament_name, m.stage_name, m.played_at, '' AS tier,
                       m.tournament_id
                FROM matches m
            """
        params = {"lim": limit, "off": offset}
        where = []
        if game:
            where.append("m.game_name = %(game)s")
            params["game"] = game
        if player:
            if player_match == "regex":
                where.append("(match(lowerUTF8(m.player1_name), lowerUTF8(%(player)s)) OR match(lowerUTF8(m.player2_name), lowerUTF8(%(player)s)))")
                params["player"] = player
            elif player_match == "partial":
                where.append("(m.player1_name ILIKE %(player)s OR m.player2_name ILIKE %(player)s)")
                params["player"] = f"%{player}%"
            else:
                pid = self._player_id(player)
                if pid is None:
                    return []
                where.append("(m.player1_id = %(pid)s OR m.player2_id = %(pid)s)")
                params["pid"] = pid
        if tournament:
            if tournament_match == "regex":
                where.append("match(lowerUTF8(m.tournament_name), lowerUTF8(%(tournament)s))")
                params["tournament"] = tournament
            elif tournament_match == "partial":
                where.append("m.tournament_name ILIKE %(tournament)s")
                params["tournament"] = f"%{tournament}%"
            else:
                where.append("m.tournament_name = %(tournament)s")
                params["tournament"] = tournament
        if tier:
            where.append("t.tier = %(tier)s")
            params["tier"] = tier
        if where:
            query += " WHERE " + " AND ".join(where)
        if sort_col:
            # Fetch ALL matching rows, sort in Python, then slice the page.
            query += " ORDER BY m.played_at DESC, m.match_id DESC LIMIT 100000"
        else:
            query += " ORDER BY m.played_at DESC, m.match_id DESC LIMIT %(lim)s OFFSET %(off)s"

        rows = self.db.client.execute(query, params)
        # When the JOIN was dropped (no tier filter), fetch tiers for the
        # returned tournament_ids in one small query and fill them in.
        if not need_join and rows:
            tids = {r[13] for r in rows if r[13]}
            tier_map = {}
            if tids:
                trows = self.db.client.execute(
                    "SELECT tournament_id, tier FROM tournaments WHERE tournament_id IN %(ids)s",
                    {"ids": tuple(tids)},
                )
                tier_map = {r[0]: r[1] for r in trows}
            rows = [r[:12] + (tier_map.get(r[13], ""),) + (r[13],) for r in rows]
        ids = {r[5] for r in rows} | {r[6] for r in rows}
        canon = self._canonical_names(list(ids))
        countries = self._countries(list(ids))
        matches = [
            {
                "match_id": r[0],
                "player1": canon.get(r[5], r[1]),
                "player2": canon.get(r[6], r[2]),
                "player1_id": r[5],
                "player2_id": r[6],
                "player1_country": countries.get(r[5], ""),
                "player2_country": countries.get(r[6], ""),
                "score": f"{r[3]}-{r[4]}",
                "score1": r[3],
                "score2": r[4],
                "winner": _winner_from_ids(r[5], r[6], r[7], canon.get(r[5], r[1]), canon.get(r[6], r[2])),
                "game": r[8],
                "tournament": r[9],
                "tournament_id": r[13],
                "stage": r[10],
                "played_at": r[11],
                "tier": r[12],
            }
            for r in rows
        ]
        if sort_col:
            matches = _sort_matches(matches, sort_col, sort_dir)
            matches = matches[offset:offset + limit]
        return matches

    def count_recent_matches(
        self,
        game: str = "",
        player: str = "",
        tournament: str = "",
        tier: str = "",
        match: str = "exact",
        player_match: Optional[str] = None,
        tournament_match: Optional[str] = None,
    ) -> int:
        """Total number of matches matching the given filters (for pagination)."""
        player_match = player_match or match
        tournament_match = tournament_match or match
        query = """
            SELECT count()
            FROM matches m
            LEFT JOIN tournaments t ON m.tournament_id = t.tournament_id
        """
        params = {}
        where = []
        if game:
            where.append("m.game_name = %(game)s")
            params["game"] = game
        if player:
            if player_match == "regex":
                where.append("(match(lowerUTF8(m.player1_name), lowerUTF8(%(player)s)) OR match(lowerUTF8(m.player2_name), lowerUTF8(%(player)s)))")
                params["player"] = player
            elif player_match == "partial":
                where.append("(m.player1_name ILIKE %(player)s OR m.player2_name ILIKE %(player)s)")
                params["player"] = f"%{player}%"
            else:
                pid = self._player_id(player)
                if pid is None:
                    return 0
                where.append("(m.player1_id = %(pid)s OR m.player2_id = %(pid)s)")
                params["pid"] = pid
        if tournament:
            if tournament_match == "regex":
                where.append("match(lowerUTF8(m.tournament_name), lowerUTF8(%(tournament)s))")
                params["tournament"] = tournament
            elif tournament_match == "partial":
                where.append("m.tournament_name ILIKE %(tournament)s")
                params["tournament"] = f"%{tournament}%"
            else:
                where.append("m.tournament_name = %(tournament)s")
                params["tournament"] = tournament
        if tier:
            where.append("t.tier = %(tier)s")
            params["tier"] = tier
        if where:
            query += " WHERE " + " AND ".join(where)
        rows = self.db.client.execute(query, params)
        return rows[0][0] if rows else 0

    def matches_col_widths(
        self,
        game: str = "",
        player: str = "",
        tournament: str = "",
        tier: str = "",
        match: str = "exact",
        player_match: Optional[str] = None,
        tournament_match: Optional[str] = None,
    ) -> dict:
        """Max content length per column across the FULL filtered dataset.

        Used to set stable min-widths on the matches table so column widths
        don't shift when sorting/paginating (auto layout only sees the current
        page's rows otherwise). Returns a dict of column -> max char length.
        """
        player_match = player_match or match
        tournament_match = tournament_match or match
        query = """
            SELECT
                max(length(m.player1_name)),
                max(length(m.player2_name)),
                max(length(m.game_name)),
                max(length(m.tournament_name)),
                max(length(t.tier)),
                max(length(m.stage_name))
            FROM matches m
            LEFT JOIN tournaments t ON m.tournament_id = t.tournament_id
        """
        params = {}
        where = []
        if game:
            where.append("m.game_name = %(game)s")
            params["game"] = game
        if player:
            if player_match == "regex":
                where.append("(match(lowerUTF8(m.player1_name), lowerUTF8(%(player)s)) OR match(lowerUTF8(m.player2_name), lowerUTF8(%(player)s)))")
                params["player"] = player
            elif player_match == "partial":
                where.append("(m.player1_name ILIKE %(player)s OR m.player2_name ILIKE %(player)s)")
                params["player"] = f"%{player}%"
            else:
                pid = self._player_id(player)
                if pid is None:
                    return {}
                where.append("(m.player1_id = %(pid)s OR m.player2_id = %(pid)s)")
                params["pid"] = pid
        if tournament:
            if tournament_match == "regex":
                where.append("match(lowerUTF8(m.tournament_name), lowerUTF8(%(tournament)s))")
                params["tournament"] = tournament
            elif tournament_match == "partial":
                where.append("m.tournament_name ILIKE %(tournament)s")
                params["tournament"] = f"%{tournament}%"
            else:
                where.append("m.tournament_name = %(tournament)s")
                params["tournament"] = tournament
        if tier:
            where.append("t.tier = %(tier)s")
            params["tier"] = tier
        if where:
            query += " WHERE " + " AND ".join(where)
        rows = self.db.client.execute(query, params)
        if not rows:
            return {}
        r = rows[0]
        return {
            "player1": r[0] or 0,
            "player2": r[1] or 0,
            "game": r[2] or 0,
            "tournament": r[3] or 0,
            "tier": r[4] or 0,
            "stage": r[5] or 0,
        }

    def tournaments_col_widths(self) -> dict:
        """Max content length per column across the FULL tournaments dataset.

        Used to set stable widths on the tournaments table so column widths
        don't shift when sorting/filtering/paginating (auto layout only sees
        the current page's rows otherwise). Returns dict of column -> max char
        length. Ignores filters so widths are always identical.
        """
        query = """
            SELECT
                max(length(name)), max(length(tier)), max(length(game_name)),
                max(length(toString(cnt))), max(length(toString(pcnt))),
                max(mcnt)
            FROM (
                SELECT
                    t.name AS name,
                    t.tier AS tier,
                    any(m.game_name) AS game_name,
                    count(m.match_id) AS cnt,
                    p.pcnt AS pcnt,
                    length(arrayDistinct(t.maplist)) AS mcnt
                FROM tournaments t FINAL
                LEFT JOIN matches m ON m.tournament_id = t.tournament_id
                LEFT JOIN (
                    SELECT tournament_id, uniqExact(pid) AS pcnt FROM (
                        SELECT tournament_id, player1_id AS pid FROM matches
                        UNION ALL
                        SELECT tournament_id, player2_id AS pid FROM matches
                    ) GROUP BY tournament_id
                ) p ON p.tournament_id = t.tournament_id
                WHERE t.name != ''
                GROUP BY t.tournament_id, t.name, t.tier, p.pcnt, t.maplist
            )
        """
        rows = self.db.client.execute(query)
        if not rows:
            return {}
        r = rows[0]
        return {
            "name": r[0] or 0,
            "tier": r[1] or 0,
            "game": r[2] or 0,
            "matches": r[3] or 0,
            "players": r[4] or 0,
            "maps": r[5] or 0,
        }

    # --- Player matches ---

    def get_player_matches(
        self,
        player_name: str,
        game: str = "",
        limit: int = 20,
        tournament: str = "",
    ) -> list[dict]:
        """Recent matches for a specific player."""
        player_id = self._player_id(player_name)
        if player_id is None:
            return []
        query = """
            SELECT m.match_id, m.player1_name, m.player2_name, m.player1_score, m.player2_score,
                   m.player1_id, m.player2_id, m.winner_id, m.game_name, m.tournament_name, m.stage_name, m.played_at, t.tier,
                   m.tournament_id
            FROM matches m
            LEFT JOIN tournaments t ON m.tournament_id = t.tournament_id
            WHERE (m.player1_id = %(pid)s OR m.player2_id = %(pid)s)
        """
        params = {"pid": player_id, "lim": limit}
        if game:
            query += " AND m.game_name = %(game)s"
            params["game"] = game
        if tournament:
            query += " AND m.tournament_name = %(tournament)s"
            params["tournament"] = tournament
        query += " ORDER BY m.played_at DESC, m.match_id DESC LIMIT %(lim)s"

        rows = self.db.client.execute(query, params)
        # Map both sides to canonical names so the same player always displays
        # under one spelling.
        ids = {r[5] for r in rows} | {r[6] for r in rows}
        canon = self._canonical_names(list(ids))
        countries = self._countries(list(ids))
        return [
            {
                "match_id": r[0],
                "player1": canon.get(r[5], r[1]),
                "player2": canon.get(r[6], r[2]),
                "player1_id": r[5],
                "player2_id": r[6],
                "player1_country": countries.get(r[5], ""),
                "player2_country": countries.get(r[6], ""),
                "score": f"{r[3]}-{r[4]}",
                "score1": r[3],
                "score2": r[4],
                "winner": _winner_from_ids(r[5], r[6], r[7], canon.get(r[5], r[1]), canon.get(r[6], r[2])),
                "game": r[8],
                "tournament": r[9],
                "tournament_id": r[13],
                "stage": r[10],
                "played_at": r[11],
                "tier": r[12],
            }
            for r in rows
        ]

    def get_match_details(self, match_id: int) -> Optional[dict]:
        """Full details for a single match: header info + per-map breakdown.

        Returns a dict with the match header (players, score, game, tournament,
        stage, tier, date, countries) and a `maps` list, each map carrying its
        name, per-player score, the winner name, and the hover image path
        (from the maps lookup table by map_id; raw HTML fallback for unknown
        maps). Returns None if the match_id doesn't exist.
        """
        rows = self.db.client.execute(
            """
            SELECT m.match_id, m.player1_name, m.player2_name, m.player1_score, m.player2_score,
                   m.player1_id, m.player2_id, m.winner_id, m.game_name, m.tournament_name,
                   m.stage_name, m.played_at, m.match_format, t.tier, m.tournament_id
            FROM matches m
            LEFT JOIN tournaments t ON m.tournament_id = t.tournament_id
            WHERE m.match_id = %(mid)s
            """,
            {"mid": match_id},
        )
        if not rows:
            return None
        r = rows[0]
        ids = {r[5], r[6]}
        canon = self._canonical_names(list(ids))
        countries = self._countries(list(ids))
        p1 = canon.get(r[5], r[1])
        p2 = canon.get(r[6], r[2])
        winner = _winner_from_ids(r[5], r[6], r[7], p1, p2)

        # Per-map breakdown (may be empty for matches without map data).
        # FINAL dedups ReplacingMergeTree rows (backfill re-inserts can leave
        # duplicate (played_at, match_id, map_index) rows until a merge).
        map_rows = self.db.client.execute(
            """
            SELECT map_index, map_name, player1_name, player2_name, player1_score, player2_score, map_id
            FROM match_maps FINAL
            WHERE match_id = %(mid)s
            ORDER BY map_index
            """,
            {"mid": match_id},
        )
        # Map hover images come from the maps lookup table (by map_id). For
        # unknown maps (map_id=0) fall back to the raw HTML data-image.
        self._load_map_images()
        images: dict[str, str] = {}
        unknown_ids = [mr[6] for mr in map_rows if mr[6] == 0]
        if unknown_ids:
            reg = self.db.client.execute(
                "SELECT raw_html FROM match_registry WHERE match_id = %(mid)s", {"mid": match_id}
            )
            if reg and reg[0][0]:
                for m in re.finditer(
                    r'data-image="([^"]+)"[^>]*data-name="([^"]+)"', reg[0][0]
                ):
                    images[m.group(2)] = m.group(1)

        maps = []
        for mr in map_rows:
            mp1 = mr[2]
            mp2 = mr[3]
            s1, s2 = mr[4], mr[5]
            mid = mr[6]
            # Map-level winner: higher frag score wins the map (draws -> None).
            mwin = mp1 if s1 > s2 else (mp2 if s2 > s1 else None)
            img = _MAP_IMAGE_CACHE.get(mid, "") if mid else images.get(mr[1], "")
            maps.append({
                "index": mr[0],
                "name": mr[1],
                "player1": mp1,
                "player2": mp2,
                "score1": s1,
                "score2": s2,
                "winner": mwin,
                "image": img,
            })

        return {
            "match_id": r[0],
            "player1": p1,
            "player2": p2,
            "player1_id": r[5],
            "player2_id": r[6],
            "player1_country": countries.get(r[5], ""),
            "player2_country": countries.get(r[6], ""),
            "score1": r[3],
            "score2": r[4],
            "winner": winner,
            "game": r[8],
            "tournament": r[9],
            "stage": r[10],
            "played_at": r[11],
            "format": r[12],
            "tier": r[13],
            "tournament_id": r[14],
            "maps": maps,
        }

    def get_ratings_before_match(self, match_id: int) -> dict:
        """Ratings each player had just before a match (per game + system).

        Returns a dict keyed by player_id, each value being a dict of
        {game_name: {system: rating}}. Ratings come from the most recent
        rating_history snapshot strictly before the match's played_at, so they
        reflect what each player was rated going into the match (not after).
        Empty dict if the match doesn't exist or no prior snapshots exist.
        """
        m = self.db.client.execute(
            "SELECT player1_id, player2_id, played_at FROM matches WHERE match_id = %(mid)s",
            {"mid": match_id},
        )
        if not m:
            return {}
        p1, p2, played_at = m[0][0], m[0][1], m[0][2]
        rows = self.db.client.execute(
            """
            SELECT player_id, game_name, rating_system, rating
            FROM rating_history
            WHERE player_id IN %(ids)s
              AND played_at < %(ts)s
            ORDER BY player_id, played_at DESC, match_id DESC
            LIMIT 1 BY player_id, game_name, rating_system
            """,
            {"ids": (p1, p2), "ts": played_at},
        )
        out: dict[int, dict[str, dict[str, float]]] = {}
        for pid, game, sys, rating in rows:
            out.setdefault(pid, {}).setdefault(game, {})[sys] = rating
        return out

    # --- Stats ---

    def get_player_rank(
        self,
        player_name: str,
        game: str = "",
        system: str = "elo",
        min_matches: int = 0,
    ) -> Optional[dict]:
        """Rank position of a player in a specific game+system leaderboard."""
        player_id = self._player_id(player_name)
        if player_id is None:
            return None
        canon = self._canonical_name(player_id)
        # Get the player's rating
        rating_row = self.db.client.execute(
            """
            SELECT rating, rd, wins, losses, matches_played
            FROM player_ratings FINAL
            WHERE player_id = %(pid)s AND game_name = %(game)s AND rating_system = %(sys)s
            LIMIT 1
            """,
            {"pid": player_id, "game": game, "sys": system},
        )
        if not rating_row:
            return None

        rating, rd, wins, losses, matches = rating_row[0]

        # If the player doesn't meet the min-matches threshold, they're not on the
        # leaderboard, so they have no rank (consistent with get_top_players).
        if min_matches > 0 and matches < min_matches:
            return None

        mm_clause = "AND matches_played >= %(mm)s" if min_matches > 0 else ""
        params = {"game": game, "sys": system, "r": rating, "mm": min_matches}

        # Glicko-2 ranks by rating - RD (conservative lower bound), matching the
        # leaderboard sort. Elo ranks by raw rating.
        if system == "glicko2":
            rank_expr = _glicko_rank_sql()
            player_val = _glicko_rank_value(rating, rd)
        else:
            rank_expr = "rating"
            player_val = rating

        # Dense rank: count distinct values strictly higher than the player's,
        # so players with identical values share the same rank.
        rank_row = self.db.client.execute(
            f"""
            SELECT count(DISTINCT {rank_expr})
            FROM player_ratings FINAL
            WHERE game_name = %(game)s AND rating_system = %(sys)s AND {rank_expr} > %(r)s
            {mm_clause}
            """,
            {**params, "r": player_val},
        )
        rank = rank_row[0][0] + 1

        total_row = self.db.client.execute(
            f"""
            SELECT count()
            FROM player_ratings FINAL
            WHERE game_name = %(game)s AND rating_system = %(sys)s
            {mm_clause}
            """,
            params,
        )
        total = total_row[0][0]

        return {
            "name": canon,
            "rank": rank,
            "total": total,
            "rating": round(rating, 1),
            "wins": wins,
            "losses": losses,
            "matches": matches,
            "system": system,
            "game": game or "All Games",
        }

    def get_player_ranks(self, player_id: int, combos: list[dict], player_ratings: list[dict] | None = None) -> dict:
        """Batched rank + total for multiple (game, system) leaderboards.

        Each combo is a dict: {game, system, min_matches}. The player's exact
        rating/rd for each (game, system) is fetched internally (one query), so
        the rank comparison uses the un-rounded stored value — matching
        get_player_rank exactly. Returns {(game, system): {'rank': int,
        'total': int}} for combos where the player qualifies (meets
        min_matches), else None for that key.

        player_ratings: optional pre-fetched list from get_player_ratings. If
        provided, the player's exact ratings are taken from it (keyed by
        (game, system)) using the un-rounded _raw_rating field, avoiding a
        separate player_ratings FINAL query — the caller (player page) already
        has these rows.

        Replaces the previous N+1 pattern of calling get_player_rank once per
        rating row (3 queries each). This runs a single query for the player's
        ratings, one for the distinct rank-values per (game, system), and one
        for the totals, then uses a binary search per combo — the same batching
        trick used for tournament rating deltas.
        """
        if not combos:
            return {}
        # Normalize game: '' (All Games) is stored as '' in the DB.
        norm = [
            {"game": c.get("game", ""), "system": c.get("system", "elo"),
             "min_matches": c.get("min_matches", 0)}
            for c in combos
        ]
        # Distinct (game, system) pairs we need values for.
        pairs = sorted({(c["game"], c["system"]) for c in norm})
        # Max min_matches across combos sharing a (game, system) — a single
        # value set per pair must satisfy the strictest threshold so every
        # combo's count is correct.
        max_mm: dict[tuple, int] = {}
        for c in norm:
            k = (c["game"], c["system"])
            max_mm[k] = max(max_mm.get(k, 0), c.get("min_matches", 0))

        # The player's exact ratings for all pairs (un-rounded, so the rank
        # comparison matches get_player_rank). Reuse caller's rows when given.
        if player_ratings is not None:
            player_rat: dict[tuple, tuple] = {}
            for r in player_ratings:
                g = "" if r["game"] == "All Games" else r["game"]
                player_rat[(g, r["system"])] = (r["_raw_rating"], r["_raw_rd"], r["matches"])
        else:
            pair_conds = []
            pparams: dict = {"pid": player_id}
            for i, (g, s) in enumerate(pairs):
                pair_conds.append(
                    f"(game_name = %(pg{i})s AND rating_system = %(ps{i})s)"
                )
                pparams[f"pg{i}"] = g
                pparams[f"ps{i}"] = s
            prow = self.db.client.execute(
                f"""
                SELECT game_name, rating_system, rating, rd, matches_played
                FROM player_ratings FINAL
                WHERE player_id = %(pid)s AND ({' OR '.join(pair_conds)})
                """,
                pparams,
            )
            player_rat: dict[tuple, tuple] = {}
            for g, s, rating, rd, matches in prow:
                player_rat[(g, s)] = (rating, rd, matches)

        # One query: distinct rank-values per (game, system), filtered by the
        # strictest min_matches for that pair. Glicko-2 uses rating - rd.
        mm_conds = []
        params: dict = {}
        for i, (g, s) in enumerate(pairs):
            mm = max_mm[(g, s)]
            if mm > 0:
                mm_conds.append(
                    f"(game_name = %(g{i})s AND rating_system = %(s{i})s AND matches_played >= %(mm{i})s)"
                )
                params[f"g{i}"] = g
                params[f"s{i}"] = s
                params[f"mm{i}"] = mm
            else:
                mm_conds.append(
                    f"(game_name = %(g{i})s AND rating_system = %(s{i})s)"
                )
                params[f"g{i}"] = g
                params[f"s{i}"] = s
        mm_where = " OR ".join(mm_conds)
        rows = self.db.client.execute(
            f"""
            SELECT game_name, rating_system,
                   if(rating_system = 'glicko2', rating - rd, rating) AS rv
            FROM player_ratings FINAL
            WHERE ({mm_where})
            """,
            params,
        )
        # Distinct sorted values per (game, system) for bisect, plus the per-
        # pair row count (total) — both derived from the same single scan,
        # avoiding a second full pass over player_ratings for the totals.
        values: dict[tuple, set] = {}
        totals: dict[tuple, int] = {}
        for g, s, rv in rows:
            values.setdefault((g, s), set()).add(rv)
            totals[(g, s)] = totals.get((g, s), 0) + 1
        for k in values:
            values[k] = sorted(values[k])

        out: dict[tuple, dict] = {}
        for c in norm:
            k = (c["game"], c["system"])
            pr = player_rat.get(k)
            if pr is None:
                out[k] = None
                continue
            rating, rd, matches = pr
            if c.get("min_matches", 0) > 0 and matches < c["min_matches"]:
                out[k] = None
                continue
            rv = _glicko_rank_value(rating, rd) if c["system"] == "glicko2" else rating
            vals = values.get(k, [])
            # Dense rank: count distinct values strictly greater than the
            # player's, +1.
            rank = len(vals) - bisect.bisect_right(vals, rv) + 1
            out[k] = {"rank": rank, "total": totals.get(k, 0)}
        return out

    def get_stats(self) -> dict:
        """Overall system stats. Cached at module level — data only changes on import."""
        global _STATS_CACHE
        if _STATS_CACHE is not None:
            return _STATS_CACHE
        # Combine the matches-table aggregates (count, date range, tournament
        # count) into a single scan instead of three separate round-trips.
        mrow = self.db.client.execute(
            """
            SELECT
                count() AS total_matches,
                min(played_at) AS first_match,
                max(played_at) AS last_match,
                count(DISTINCT tournament_id) AS tournaments
            FROM matches
            """
        )[0]
        total_matches = mrow[0]
        date_range = (mrow[1], mrow[2])
        tournaments = mrow[3]
        total_players = self.db.client.execute("SELECT count() FROM players FINAL")[0][0]
        # Combine the match_registry counts (downloaded + discovered) into one
        # query instead of two round-trips.
        reg = self.db.client.execute(
            """
            SELECT
                countIf(status = 'parsed') AS downloaded,
                uniqExact(match_id) AS discovered
            FROM match_registry
            """
        )[0]
        total_downloaded = reg[0]
        total_discovered = reg[1]
        games = self.get_games()

        # Combined: per-game matches/tournaments/players in one query (saves 1
        # round-trip vs separate per_game + players_per_game queries).
        per_game = self.db.client.execute(
            """
            SELECT
                game_name,
                count(DISTINCT match_id) AS matches,
                count(DISTINCT tournament_id) AS tournaments,
                count(DISTINCT player_id) AS players
            FROM (
                SELECT game_name, match_id, tournament_id, player1_id AS player_id FROM matches
                UNION ALL
                SELECT game_name, match_id, tournament_id, player2_id AS player_id FROM matches
            )
            GROUP BY game_name
            ORDER BY matches DESC
            """
        )
        matches_per_game = {name: matches for name, matches, _, _ in per_game}
        tournaments_per_game = {name: tcnt for name, _, tcnt, _ in per_game}
        players_per_game = {name: pcount for name, _, _, pcount in per_game}

        # Combined: active players (last 30 days) + countries in one query.
        sc = self.db.client.execute(
            """
            SELECT
                count(DISTINCT if(played_at >= now() - INTERVAL 30 DAY, player_id, NULL)) AS active_players,
                count(DISTINCT if(country != '', country, NULL)) AS countries
            FROM (
                SELECT played_at, player1_id AS player_id, player1_country AS country FROM matches
                UNION ALL
                SELECT played_at, player2_id AS player_id, player2_country AS country FROM matches
            )
            """
        )[0]
        active_players = sc[0]
        countries = sc[1]

        # Avg matches per player
        avg_matches = round(total_matches / total_players, 1) if total_players > 0 else 0

        result = {
            "total_matches": total_matches,
            "total_players": total_players,
            "total_downloaded": total_downloaded,
            "total_discovered": total_discovered,
            "games": games,
            "date_range": (date_range[0], date_range[1]),
            "matches_per_game": matches_per_game,
            "tournaments_per_game": tournaments_per_game,
            "players_per_game": players_per_game,
            "active_players": active_players,
            "avg_matches": avg_matches,
            "tournaments": tournaments,
            "countries": countries,
        }
        _STATS_CACHE = result
        return result

    def get_most_active_players(self, limit: int = 10) -> list[dict]:
        """Players with the most matches (all games, any system)."""
        rows = self.db.client.execute(
            """
            SELECT player_id, player_name, matches_played, wins, losses
            FROM player_ratings
            WHERE rating_system = 'elo' AND game_name = ''
            ORDER BY matches_played DESC
            LIMIT %(lim)s
            """,
            {"lim": limit},
        )
        canon = self._canonical_names([r[0] for r in rows])
        countries = self._countries([r[0] for r in rows])
        return [
            {
                "player_id": r[0],
                "name": canon.get(r[0], r[1]),
                "country": countries.get(r[0], ""),
                "matches": r[2],
                "wins": r[3],
                "losses": r[4],
            }
            for r in rows
        ]

    def get_peak_rating_overall(self, system: str = "elo", min_matches: int = 0) -> dict | None:
        """The single highest peak rating across all players and games.

        For glicko2, the peak is computed with RD removed (rating - rd, the
        conservative lower bound), matching the leaderboard sort behavior.
        If min_matches > 0, only players with at least that many matches at
        the moment they reached their peak are considered (matching the
        leaderboard's min-matches filter).
        """
        if system == "glicko2":
            rank_expr = _glicko_rank_sql("h")
            if min_matches > 0:
                rows = self.db.client.execute(
                    f"""
                    SELECT h.player_id, max({rank_expr}) AS peak,
                           argMax(h.played_at, {rank_expr}) AS peak_date,
                           argMax(h.game_name, {rank_expr}) AS peak_game
                    FROM rating_history h
                    WHERE h.rating_system = %(rs)s AND h.matches_played >= %(mm)s
                    GROUP BY h.player_id
                    ORDER BY peak DESC
                    LIMIT 1
                    """,
                    {"rs": system, "mm": min_matches},
                )
            else:
                rows = self.db.client.execute(
                    f"""
                    SELECT h.player_id, max({rank_expr}) AS peak,
                           argMax(h.played_at, {rank_expr}) AS peak_date,
                           argMax(h.game_name, {rank_expr}) AS peak_game
                    FROM rating_history h
                    WHERE h.rating_system = %(rs)s
                    GROUP BY h.player_id
                    ORDER BY peak DESC
                    LIMIT 1
                    """,
                    {"rs": system},
                )
        else:
            if min_matches > 0:
                rows = self.db.client.execute(
                    """
                    SELECT h.player_id, max(h.rating) AS peak, argMax(h.played_at, h.rating) AS peak_date,
                           argMax(h.game_name, h.rating) AS peak_game
                    FROM rating_history h
                    WHERE h.rating_system = %(rs)s AND h.matches_played >= %(mm)s
                    GROUP BY h.player_id
                    ORDER BY peak DESC
                    LIMIT 1
                    """,
                    {"rs": system, "mm": min_matches},
                )
            else:
                rows = self.db.client.execute(
                    """
                    SELECT h.player_id, max(h.rating) AS peak, argMax(h.played_at, h.rating) AS peak_date,
                           argMax(h.game_name, h.rating) AS peak_game
                    FROM rating_history h
                    WHERE h.rating_system = %(rs)s
                    GROUP BY h.player_id
                    ORDER BY peak DESC
                    LIMIT 1
                    """,
                    {"rs": system},
                )
        if not rows:
            return None
        pid, peak, peak_date, peak_game = rows[0]
        # Fetch canonical name
        name = self._canonical_name(pid)
        return {
            "player_id": pid,
            "name": name,
            "country": self._country(pid),
            "peak": round(peak, 1),
            "peak_date": peak_date,
            "game": peak_game or "All Games",
        }

    def get_peak_rating_overall_both(self, min_matches: dict | int = 0) -> dict:
        """Elo + Glicko-2 overall peaks in a SINGLE scan of rating_history.

        Equivalent to calling get_peak_rating_overall twice (once per system),
        but scans rating_history once instead of twice. Returns
        {'elo': {...}|None, 'glicko2': {...}|None} where each value is the same
        shape get_peak_rating_overall returns. min_matches may be a dict
        {'elo': n, 'glicko2': n} for per-system thresholds, or a single int
        applied to both.

        Results are cached per (mm_elo, mm_glicko) key at module level — peak
        ratings only change when new data is imported (separate process).
        """
        if isinstance(min_matches, dict):
            mm_elo = min_matches.get("elo", 0)
            mm_glicko = min_matches.get("glicko2", 0)
        else:
            mm_elo = mm_glicko = min_matches
        cache_key = (mm_elo, mm_glicko)
        cached = _PEAK_OVERALL_CACHE.get(cache_key)
        if cached is not None:
            return cached
        rows = self.db.client.execute(
            """
            SELECT rating_system, player_id,
                   max(if(rating_system = 'glicko2', h.rating - h.rd, h.rating)) AS peak,
                   argMax(h.played_at, if(rating_system = 'glicko2', h.rating - h.rd, h.rating)) AS peak_date,
                   argMax(h.game_name, if(rating_system = 'glicko2', h.rating - h.rd, h.rating)) AS peak_game
            FROM rating_history h
            WHERE h.rating_system IN ('elo', 'glicko2')
              AND h.matches_played >= if(rating_system = 'glicko2', %(mmg)s, %(mme)s)
            GROUP BY rating_system, h.player_id
            ORDER BY peak DESC
            LIMIT 1 BY rating_system
            """,
            {"mme": mm_elo, "mmg": mm_glicko},
        )
        out: dict = {}
        for sys, pid, peak, peak_date, peak_game in rows:
            out[sys] = {
                "player_id": pid,
                "name": self._canonical_name(pid),
                "country": self._country(pid),
                "peak": round(peak, 1),
                "peak_date": peak_date,
                "game": peak_game or "All Games",
            }
        _PEAK_OVERALL_CACHE[cache_key] = out
        return out

    def get_top_players_by_game(self, system: str = "elo", limit: int = 5) -> list[dict]:
        """Top player per game (for dashboard mini-lists)."""
        games = self.get_games()
        result = []
        for g in games:
            top = self.get_top_players(game=g, system=system, limit=limit, min_matches=0)
            if top:
                result.append({"game": g, "players": top})
        return result

    def get_player_summary(
        self,
        player_name: str,
        ratings: list[dict] | None = None,
        recent_matches: list[dict] | None = None,
    ) -> dict | None:
        """Aggregated summary stats for a player.

        ratings: optional pre-fetched list from get_player_ratings (avoids a
        redundant re-query when the caller already has the rows, e.g. the
        player page). If omitted, ratings are fetched here.
        recent_matches: optional pre-fetched list from get_player_matches
        (limit >= 100) used for streak detection. If omitted, fetched here.
        """
        player_name = self._resolve_name(player_name)
        p_id = self._player_id(player_name)
        # Get all ratings (reuse caller's rows when provided)
        ratings = ratings if ratings is not None else self.get_player_ratings(player_name)
        if not ratings:
            return None

        # Find best game (highest rating) and worst game (lowest rating)
        best_game = None
        worst_game = None
        best_rating = -999999
        worst_rating = 999999
        peak_rating = 0
        peak_date = None
        peak_glicko = 0
        peak_glicko_date = None
        total_wins = 0
        total_losses = 0
        total_matches = 0
        for r in ratings:
            if r["system"] == "elo" and r["game"] == "All Games":
                total_wins = r["wins"]
                total_losses = r["losses"]
                total_matches = r["matches"]
                if r["peak"]:
                    peak_rating = r["peak"]
                    peak_date = r["peak_date"]
            if r["system"] == "glicko2" and r["game"] == "All Games":
                if r["peak"]:
                    peak_glicko = r["peak"]
                    peak_glicko_date = r["peak_date"]
            if r["system"] == "elo" and r["game"] != "All Games":
                if r["rating"] > best_rating:
                    best_rating = r["rating"]
                    best_game = r
                if r["rating"] < worst_rating:
                    worst_rating = r["rating"]
                    worst_game = r

        # Current streak from recent matches (scan enough to catch long streaks)
        recent = recent_matches if recent_matches is not None else self.get_player_matches(player_name, limit=100)
        streak = 0
        streak_type = ""
        for m in recent:
            if m["winner"] == player_name:
                if streak_type == "" or streak_type == "W":
                    streak_type = "W"
                    streak += 1
                else:
                    break
            else:
                if streak_type == "" or streak_type == "L":
                    streak_type = "L"
                    streak += 1
                else:
                    break

        # Per-game breakdown
        per_game = {}
        for r in ratings:
            if r["system"] == "elo" and r["game"] != "All Games":
                g = r["game"]
                per_game[g] = {
                    "rating": r["rating"],
                    "peak": r["peak"],
                    "wins": r["wins"],
                    "losses": r["losses"],
                    "matches": r["matches"],
                    "win_rate": round(r["wins"] / r["matches"] * 100, 1) if r["matches"] > 0 else 0,
                }

        win_rate = round(total_wins / total_matches * 100, 1) if total_matches > 0 else 0

        # First / last match dates (when the player started and stopped playing)
        date_rows = self.db.client.execute(
            """
            SELECT min(played_at) AS first_match, max(played_at) AS last_match
            FROM matches
            WHERE player1_id = %(pid)s OR player2_id = %(pid)s
            """,
            {"pid": p_id},
        )
        first_match = date_rows[0][0] if date_rows else None
        last_match = date_rows[0][1] if date_rows else None

        # Rivals — top 10 most-faced opponents. Wins use the authoritative
        # winner_id (a player_id) so draws are handled correctly.
        rival_rows = self.db.client.execute(
            """
            SELECT
                CASE WHEN player1_id = %(pid)s THEN player2_id ELSE player1_id END AS opp_id,
                count() AS matches,
                sum(CASE WHEN winner_id = %(pid)s THEN 1 ELSE 0 END) AS wins
            FROM matches
            WHERE player1_id = %(pid)s OR player2_id = %(pid)s
            GROUP BY opp_id
            ORDER BY matches DESC
            LIMIT 10
            """,
            {"pid": p_id},
        )
        opp_ids = [r[0] for r in rival_rows]
        opp_canon = self._canonical_names(opp_ids)
        opp_countries = self._countries(opp_ids)
        rivals = [
            {
                "name": opp_canon.get(r[0], f"player_{r[0]}"),
                "id": r[0],
                "country": opp_countries.get(r[0], ""),
                "matches": r[1],
                "wins": r[2],
                "losses": r[1] - r[2],
            }
            for r in rival_rows
        ]
        # Sort rivals by least win rate first (hardest opponents on top).
        rivals.sort(key=lambda r: (r["wins"] / r["matches"] if r["matches"] > 0 else 0))

        return {
            "name": ratings[0]["name"],
            "total_wins": total_wins,
            "total_losses": total_losses,
            "total_matches": total_matches,
            "win_rate": win_rate,
            "peak_rating": peak_rating,
            "peak_date": peak_date,
            "peak_glicko": peak_glicko,
            "peak_glicko_date": peak_glicko_date,
            "best_game": best_game,
            "worst_game": worst_game,
            "streak": streak,
            "streak_type": streak_type,
            "first_match": first_match,
            "last_match": last_match,
            "per_game": per_game,
            "rivals": rivals,
            "aliases": self._aliases(p_id),
        }

    def get_tournament_top_players(self, tournament_name: str, limit: int = 10) -> list[dict]:
        """Top players by wins in a specific tournament.

        Each match contributes one row per participant; a win is attributed to
        the player whose player_id matches the authoritative winner_id.
        """
        rows = self.db.client.execute(
            """
            SELECT pid, count() AS matches, sum(is_win) AS wins
            FROM (
                SELECT player1_id AS pid, winner_id,
                       if(winner_id = player1_id, 1, 0) AS is_win
                FROM matches
                WHERE tournament_name = %(t)s AND player1_id != 0
                UNION ALL
                SELECT player2_id AS pid, winner_id,
                       if(winner_id = player2_id, 1, 0) AS is_win
                FROM matches
                WHERE tournament_name = %(t)s AND player2_id != 0
            )
            GROUP BY pid
            ORDER BY wins DESC
            LIMIT %(lim)s
            """,
            {"t": tournament_name, "lim": limit},
        )
        canon = self._canonical_names([r[0] for r in rows])
        return [
            {"name": canon.get(r[0], f"player_{r[0]}"), "wins": r[2], "matches": r[1]}
            for r in rows
        ]

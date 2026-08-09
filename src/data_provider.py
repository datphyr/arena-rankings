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

import logging
from typing import Optional

from src.db_client import Database
from src.table import Col, fmt_date, fmt_rating, fmt_wr, fmt_rd, fmt_vol, fmt_tier


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


class DataProvider:
    """Common data access layer for all consumers."""

    def __init__(self, db: Optional[Database] = None):
        self.db = db or Database()
        self._owns_db = db is None

    def close(self):
        if self._owns_db:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _resolve_name(self, name: str) -> str:
        """Resolve a player name case-insensitively. Returns the canonical name, or the input if no match."""
        row = self.db.client.execute(
            "SELECT player_name FROM player_ratings FINAL WHERE lowerUTF8(player_name) = lowerUTF8(%(n)s) LIMIT 1",
            {"n": name},
        )
        if row:
            return row[0][0]
        # Fall back to matches table (player might not have ratings yet)
        row = self.db.client.execute(
            "SELECT player1_name FROM matches FINAL WHERE lowerUTF8(player1_name) = lowerUTF8(%(n)s) LIMIT 1",
            {"n": name},
        )
        if row:
            return row[0][0]
        row = self.db.client.execute(
            "SELECT player2_name FROM matches FINAL WHERE lowerUTF8(player2_name) = lowerUTF8(%(n)s) LIMIT 1",
            {"n": name},
        )
        if row:
            return row[0][0]
        return name

    # --- Games ---

    def get_games(self) -> list[str]:
        """Return list of game names (excluding combined)."""
        rows = self.db.client.execute(
            "SELECT DISTINCT game_name FROM matches FINAL WHERE game_name != '' ORDER BY game_name"
        )
        return [r[0] for r in rows]

    # --- Tournaments ---

    def get_tournaments(self, tier: str = "", limit: int = 50) -> list[dict]:
        """List tournaments, optionally filtered by tier."""
        params = {"lim": limit}
        if tier:
            params["tier"] = tier
            rows = self.db.client.execute(
                """
                SELECT t.tournament_id, t.name, t.tier,
                       min(m.played_at) AS first_match, max(m.played_at) AS last_match,
                       any(m.game_name) AS game
                FROM tournaments t FINAL
                LEFT JOIN matches m ON m.tournament_id = t.tournament_id
                WHERE t.tier = %(tier)s AND t.name != ''
                GROUP BY t.tournament_id, t.name, t.tier
                ORDER BY last_match DESC
                LIMIT %(lim)s
                """,
                params,
            )
        else:
            rows = self.db.client.execute(
                """
                SELECT t.tournament_id, t.name, t.tier,
                       min(m.played_at) AS first_match, max(m.played_at) AS last_match,
                       any(m.game_name) AS game
                FROM tournaments t FINAL
                LEFT JOIN matches m ON m.tournament_id = t.tournament_id
                WHERE t.name != ''
                GROUP BY t.tournament_id, t.name, t.tier
                ORDER BY last_match DESC
                LIMIT %(lim)s
                """,
                params,
            )
        # Count matches per tournament
        t_ids = [r[0] for r in rows]
        match_counts = {}
        if t_ids:
            mc_rows = self.db.client.execute(
                "SELECT tournament_id, count() FROM matches FINAL "
                "WHERE tournament_id IN %(ids)s GROUP BY tournament_id",
                {"ids": tuple(t_ids)},
            )
            match_counts = {r[0]: r[1] for r in mc_rows}
        return [
            {
                "tournament_id": r[0],
                "name": r[1],
                "tier": r[2],
                "first_match": r[3],
                "last_match": r[4],
                "matches": match_counts.get(r[0], 0),
                "game": r[5] or "",
            }
            for r in rows
        ]

    def get_tournament_stats(self) -> dict:
        """Tier distribution stats."""
        rows = self.db.client.execute(
            """
            SELECT tier, count() AS tournaments, sum(matches) AS matches
            FROM (
                SELECT t.tier AS tier, t.tournament_id AS tid, count(m.match_id) AS matches
                FROM tournaments t FINAL
                INNER JOIN matches m ON m.tournament_id = t.tournament_id
                WHERE t.name != ''
                GROUP BY t.tier, t.tournament_id
            )
            GROUP BY tier
            ORDER BY matches DESC
            """
        )
        return {
            "tiers": [
                {"tier": r[0], "tournaments": r[1], "matches": r[2]}
                for r in rows
            ]
        }

    # --- Leaderboards ---

    def _fetch_peaks(self, player_ids: list[int], system: str) -> dict:
        """Fetch peak rating + date for a batch of players from rating_history.

        Returns dict: {(player_id, game_name): (peak_rating, peak_date)}
        """
        if not player_ids:
            return {}
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
        return {(r[0], r[1]): (round(r[2], 1), r[3]) for r in peak_rows}

    def get_top_players(
        self,
        game: str = "",
        system: str = "elo",
        limit: int = 20,
        min_matches: int = 0,
        sort_by: str = "rating",
    ) -> list[dict]:
        """Top players by rating or peak.

        Args:
            game: game name ("" = combined)
            system: "elo" or "glicko2"
            limit: max results
            min_matches: minimum matches played to qualify
            sort_by: "rating" (current rating) or "peak" (all-time peak)
        """
        if sort_by == "peak":
            return self._get_top_players_by_peak(
                game=game, system=system, limit=limit, min_matches=min_matches
            )

        params = {"sys": system, "game": game, "lim": limit}
        if min_matches > 0:
            params["mm"] = min_matches

        if not game:
            # Combined: fetch main game per player via separate query
            query = """
                SELECT player_id, player_name, rating, rd, vol, wins, losses, matches_played,
                       last_match_date, first_match_date
                FROM player_ratings FINAL
                WHERE rating_system = %(sys)s AND game_name = ''
            """
            if min_matches > 0:
                query += " AND matches_played >= %(mm)s"
            # Glicko-2: sort by rating - RD (conservative lower bound) to demote uncertain players
            if system == "glicko2":
                query += " ORDER BY rating - rd DESC LIMIT %(lim)s"
            else:
                query += " ORDER BY rating DESC LIMIT %(lim)s"
            rows = self.db.client.execute(query, params)

            # Batch-fetch main game (most matches) for all player IDs
            player_ids = [r[0] for r in rows]
            main_games = {}
            peaks = {}
            if player_ids:
                mg_rows = self.db.client.execute(
                    "SELECT player_id, argMax(game_name, matches_played) AS main_game "
                    "FROM player_ratings FINAL "
                    "WHERE rating_system = %(sys)s AND game_name != '' AND player_id IN %(ids)s "
                    "GROUP BY player_id",
                    {"sys": system, "ids": tuple(player_ids)},
                )
                main_games = {r[0]: r[1] for r in mg_rows}
                peaks = self._fetch_peaks(player_ids, system)

            return [
                {
                    "player_id": r[0],
                    "name": r[1],
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
                }
                for r in rows
            ]
        else:
            query = """
                SELECT player_id, player_name, rating, rd, vol, wins, losses, matches_played,
                       last_match_date, first_match_date
                FROM player_ratings FINAL
                WHERE rating_system = %(sys)s AND game_name = %(game)s
            """
            if min_matches > 0:
                query += " AND matches_played >= %(mm)s"
            # Glicko-2: sort by rating - RD (conservative lower bound)
            if system == "glicko2":
                query += " ORDER BY rating - rd DESC LIMIT %(lim)s"
            else:
                query += " ORDER BY rating DESC LIMIT %(lim)s"
            rows = self.db.client.execute(query, params)
            player_ids = [r[0] for r in rows]
            peaks = self._fetch_peaks(player_ids, system)
            return [
                {
                    "player_id": r[0],
                    "name": r[1],
                    "rating": round(r[2], 1),
                    "rd": round(r[3], 1) if r[3] else None,
                    "vol": round(r[4], 4) if r[4] else None,
                    "wins": r[5],
                    "losses": r[6],
                    "matches": r[7],
                    "last_match_date": r[8],
                    "first_match_date": r[9],
                    "main_game": "",
                    "peak": peaks.get((r[0], game), (None, None))[0],
                    "peak_date": peaks.get((r[0], game), (None, None))[1],
                }
                for r in rows
            ]

    def get_ratings_for_players(
        self, player_ids: list, system: str = "glicko2", game: str = ""
    ) -> dict:
        """Batch-fetch a rating (default glicko2, combined) for a set of player IDs.

        Returns {player_id: rating_value}.
        """
        if not player_ids:
            return {}
        rows = self.db.client.execute(
            """
            SELECT player_id, rating
            FROM player_ratings FINAL
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
    ) -> list[dict]:
        """Top players by all-time peak rating (from rating_history)."""
        params = {"rs": system, "game": game, "lim": limit}
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
                    INNER JOIN (SELECT player_id FROM player_ratings FINAL WHERE rating_system = %(rs)s AND {game_filter} AND matches_played >= %(mm)s) pr ON rh.player_id = pr.player_id
                    WHERE rh.rating_system = %(rs)s AND rh.{game_filter}
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
                    INNER JOIN (SELECT player_id FROM player_ratings FINAL WHERE rating_system = %(rs)s AND {game_filter} AND matches_played >= %(mm)s) pr ON rh.player_id = pr.player_id
                    WHERE rh.rating_system = %(rs)s AND rh.{game_filter}
                    GROUP BY rh.player_id
                """
            peak_query += " ORDER BY peak DESC LIMIT %(lim)s"
        peak_rows = self.db.client.execute(peak_query, params)
        if not peak_rows:
            return []

        player_ids = [r[0] for r in peak_rows]
        peak_map = {r[0]: (round(r[1], 1), r[2]) for r in peak_rows}

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
        for pid in player_ids:
            r = rating_map.get(pid)
            pk, pkd = peak_map[pid]
            if r:
                results.append({
                    "player_id": r[0],
                    "name": r[1],
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
                })
            else:
                # Player has history but no current rating (e.g. retired)
                # Fetch name from player_ratings (any game)
                name_row = self.db.client.execute(
                    "SELECT player_name FROM player_ratings FINAL WHERE player_id = %(pid)s LIMIT 1",
                    {"pid": pid},
                )
                name = name_row[0][0] if name_row else f"player_{pid}"
                results.append({
                    "player_id": pid,
                    "name": name,
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
                })
        return results

    def get_top_players_asof(
        self,
        date: str,
        game: str = "",
        system: str = "elo",
        limit: int = 20,
        min_matches: int = 0,
        sort_by: str = "rating",
    ) -> list[dict]:
        """Top players by rating as of a specific date.

        Uses rating_history snapshots to reconstruct the leaderboard at a point in time.
        For each player, takes their latest rating snapshot at or before the given date.

        Args:
            date: ISO date string (e.g. '2020-01-01').
            game: game name ("" = combined).
            system: "elo" or "glicko2".
            limit: max results.
            min_matches: minimum matches played to qualify.
            sort_by: "rating" or "peak".
        """
        params = {"rs": system, "game": game, "lim": limit, "date": f"{date} 00:00:00"}
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
            query = f"""
                SELECT h.player_id,
                       argMax(h.rating, h.played_at) AS rating,
                       argMax(h.rd, h.played_at) AS rd,
                       argMax(h.vol, h.played_at) AS vol,
                       argMax(h.wins, h.played_at) AS wins,
                       argMax(h.losses, h.played_at) AS losses,
                       argMax(h.matches_played, h.played_at) AS matches,
                       max(h.played_at) AS last_match,
                       p.peak,
                       p.peak_date
                FROM rating_history h
                INNER JOIN (
                    {peak_subquery}
                ) p ON h.player_id = p.player_id
                WHERE h.rating_system = %(rs)s AND h.{game_filter} AND h.played_at <= toDateTime(%(date)s)
                GROUP BY h.player_id, p.peak, p.peak_date{group_extra}
            """
        else:
            order_expr = "rating - rd DESC" if system == "glicko2" else "rating DESC"
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
            # Peak as of the given date — only consider history up to that date
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
            peaks = {(r[0], r[1]): (round(r[2], 1), r[3]) for r in peak_rows}

        # For combined: also get main game per player
        main_games = {}
        if not game and player_ids:
            mg_rows = self.db.client.execute(
                "SELECT player_id, argMax(game_name, matches_played) AS main_game "
                "FROM player_ratings FINAL WHERE rating_system = %(rs)s AND game_name != '' AND player_id IN %(ids)s GROUP BY player_id",
                {"rs": system, "ids": tuple(player_ids)},
            )
            main_games = {r[0]: r[1] for r in mg_rows}

        return [
            {
                "player_id": r[0],
                "name": names.get(r[0], f"player_{r[0]}"),
                "rating": round(r[1], 1),
                "rd": round(r[2], 1) if r[2] else None,
                "vol": round(r[3], 4) if r[3] else None,
                "wins": r[4],
                "losses": r[5],
                "matches": r[6],
                "last_match_date": r[7],
                "first_match_date": first_dates.get(r[0]),
                "main_game": main_games.get(r[0], ""),
                "peak": peaks.get((r[0], game), (None, None))[0],
                "peak_date": peaks.get((r[0], game), (None, None))[1],
            }
            for r in rows
        ]

    # --- Player lookup ---

    def get_player_ratings(self, player_name: str) -> list[dict]:
        """All ratings for a player across games and systems."""
        player_name = self._resolve_name(player_name)
        rows = self.db.client.execute(
            """
            SELECT player_id, player_name, game_name, rating_system, rating, rd, vol,
                   wins, losses, matches_played, last_match_id, last_match_date, first_match_date
            FROM player_ratings FINAL
            WHERE player_name = %(name)s
            ORDER BY rating_system, rating DESC
            """,
            {"name": player_name},
        )

        player_id = rows[0][0] if rows else None
        peaks = {}
        if player_id:
            peak_rows = self.db.client.execute(
                """
                SELECT rating_system, game_name,
                       max(rating) AS peak,
                       argMax(played_at, rating) AS peak_date
                FROM rating_history
                WHERE player_id = %(pid)s
                GROUP BY rating_system, game_name
                """,
                {"pid": player_id},
            )
            for r in peak_rows:
                peaks[(r[0], r[1])] = (round(r[2], 1), r[3])

        return [
            {
                "player_id": r[0],
                "name": r[1],
                "game": r[2] or "Combined",
                "system": r[3],
                "rating": round(r[4], 1),
                "rd": round(r[5], 1) if r[5] else None,
                "vol": round(r[6], 4) if r[6] else None,
                "wins": r[7],
                "losses": r[8],
                "matches": r[9],
                "last_match_id": r[10],
                "last_match_date": r[11],
                "first_match_date": r[12],
                "peak": peaks.get((r[3], r[2]), (None, None))[0],
                "peak_date": peaks.get((r[3], r[2]), (None, None))[1],
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
        player_name = self._resolve_name(player_name)
        rows = self.db.client.execute(
            """
            SELECT match_id, played_at, rating, rd, vol, wins, losses, matches_played
            FROM rating_history
            WHERE player_id = (
                SELECT player_id FROM player_ratings FINAL
                WHERE player_name = %(name)s AND game_name = %(game)s AND rating_system = %(sys)s
                LIMIT 1
            )
            AND rating_system = %(sys)s AND game_name = %(game)s
            ORDER BY played_at DESC, match_id DESC
            LIMIT %(lim)s
            """,
            {"name": player_name, "game": game, "sys": system, "lim": limit},
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
        player_name = self._resolve_name(player_name)
        ch_fn = _glicko_period_ch_fn()
        since_clause = ""
        params: dict = {"name": player_name, "game": game, "lim": limit}
        if since:
            since_clause = "AND e.played_at >= %(since)s"
            params["since"] = since
        rows = self.db.client.execute(
            f"""
            SELECT e.match_id, e.played_at, e.rating,
                   g.rating, g.rd, g.vol,
                   e.wins, e.losses, e.matches_played
            FROM rating_history e
            ASOF LEFT JOIN rating_history g
                ON e.player_id = g.player_id
                AND e.game_name = g.game_name
                AND g.rating_system = 'glicko2'
                AND {ch_fn}(e.played_at) >= {ch_fn}(g.played_at)
            WHERE e.player_id = (
                SELECT player_id FROM player_ratings FINAL
                WHERE player_name = %(name)s AND game_name = %(game)s AND rating_system = 'elo'
                LIMIT 1
            )
            AND e.rating_system = 'elo' AND e.game_name = %(game)s
            {since_clause}
            ORDER BY e.played_at DESC, e.match_id DESC
            LIMIT %(lim)s
            """,
            params,
        )
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
        limit: int = 20,
    ) -> dict:
        """Head-to-head record between two players."""
        player1 = self._resolve_name(player1)
        player2 = self._resolve_name(player2)
        if game:
            rows = self.db.client.execute(
                """
                SELECT m.match_id, m.player1_name, m.player2_name, m.player1_score, m.player2_score,
                       m.winner_id, m.game_name, m.tournament_name, m.stage_name, m.played_at, t.tier
                FROM matches m FINAL
                LEFT JOIN tournaments t ON m.tournament_id = t.tournament_id
                WHERE m.game_name = %(game)s
                AND ((m.player1_name = %(p1)s AND m.player2_name = %(p2)s)
                  OR (m.player1_name = %(p2)s AND m.player2_name = %(p1)s))
                ORDER BY m.played_at DESC
                LIMIT %(lim)s
                """,
                {"p1": player1, "p2": player2, "game": game, "lim": limit},
            )
        else:
            rows = self.db.client.execute(
                """
                SELECT m.match_id, m.player1_name, m.player2_name, m.player1_score, m.player2_score,
                       m.winner_id, m.game_name, m.tournament_name, m.stage_name, m.played_at, t.tier
                FROM matches m FINAL
                LEFT JOIN tournaments t ON m.tournament_id = t.tournament_id
                WHERE (m.player1_name = %(p1)s AND m.player2_name = %(p2)s)
                   OR (m.player1_name = %(p2)s AND m.player2_name = %(p1)s)
                ORDER BY m.played_at DESC
                LIMIT %(lim)s
                """,
                {"p1": player1, "p2": player2, "lim": limit},
            )
        matches = []
        p1_wins = 0
        p2_wins = 0
        for r in rows:
            mid, p1n, p2n, s1, s2, wid, gn, tn, st, pa, tier = r
            # Determine winner from scores
            if p1n == player1:
                w = player1 if s1 > s2 else player2
            else:
                w = player2 if s2 > s1 else player1
            if w == player1:
                p1_wins += 1
            else:
                p2_wins += 1
            matches.append({
                "match_id": mid,
                "player1": p1n,
                "player2": p2n,
                "score": f"{s1}-{s2}",
                "winner": w,
                "game": gn,
                "tournament": tn,
                "stage": st,
                "played_at": pa,
                "tier": tier,
            })
        return {
            "player1": player1,
            "player2": player2,
            "p1_wins": p1_wins,
            "p2_wins": p2_wins,
            "total": len(matches),
            "matches": matches,
        }

    # --- Recent matches ---

    def get_recent_matches(
        self,
        game: str = "",
        limit: int = 20,
    ) -> list[dict]:
        """Recent matches (newest first)."""
        query = """
            SELECT m.match_id, m.player1_name, m.player2_name, m.player1_score, m.player2_score,
                   m.winner_id, m.game_name, m.tournament_name, m.stage_name, m.played_at, t.tier
            FROM matches m FINAL
            LEFT JOIN tournaments t ON m.tournament_id = t.tournament_id
        """
        params = {"lim": limit}
        if game:
            query += " WHERE m.game_name = %(game)s"
            params["game"] = game
        query += " ORDER BY m.played_at DESC, m.match_id DESC LIMIT %(lim)s"

        rows = self.db.client.execute(query, params)
        return [
            {
                "match_id": r[0],
                "player1": r[1],
                "player2": r[2],
                "score": f"{r[3]}-{r[4]}",
                "winner": r[1] if r[3] > r[4] else r[2],
                "game": r[6],
                "tournament": r[7],
                "stage": r[8],
                "played_at": r[9],
                "tier": r[10],
            }
            for r in rows
        ]

    # --- Player matches ---

    def get_player_matches(
        self,
        player_name: str,
        game: str = "",
        limit: int = 20,
    ) -> list[dict]:
        """Recent matches for a specific player."""
        player_name = self._resolve_name(player_name)
        query = """
            SELECT m.match_id, m.player1_name, m.player2_name, m.player1_score, m.player2_score,
                   m.winner_id, m.game_name, m.tournament_name, m.stage_name, m.played_at, t.tier
            FROM matches m FINAL
            LEFT JOIN tournaments t ON m.tournament_id = t.tournament_id
            WHERE (m.player1_name = %(name)s OR m.player2_name = %(name)s)
        """
        params = {"name": player_name, "lim": limit}
        if game:
            query += " AND m.game_name = %(game)s"
            params["game"] = game
        query += " ORDER BY m.played_at DESC, m.match_id DESC LIMIT %(lim)s"

        rows = self.db.client.execute(query, params)
        return [
            {
                "match_id": r[0],
                "player1": r[1],
                "player2": r[2],
                "score": f"{r[3]}-{r[4]}",
                "winner": r[1] if r[3] > r[4] else r[2],
                "game": r[6],
                "tournament": r[7],
                "stage": r[8],
                "played_at": r[9],
                "tier": r[10],
            }
            for r in rows
        ]

    # --- Stats ---

    def get_player_rank(
        self,
        player_name: str,
        game: str = "",
        system: str = "elo",
        min_matches: int = 0,
    ) -> Optional[dict]:
        """Rank position of a player in a specific game+system leaderboard."""
        player_name = self._resolve_name(player_name)
        # Get the player's rating
        rating_row = self.db.client.execute(
            """
            SELECT rating, wins, losses, matches_played
            FROM player_ratings FINAL
            WHERE player_name = %(name)s AND game_name = %(game)s AND rating_system = %(sys)s
            LIMIT 1
            """,
            {"name": player_name, "game": game, "sys": system},
        )
        if not rating_row:
            return None

        rating, wins, losses, matches = rating_row[0]

        mm_clause = "AND matches_played >= %(mm)s" if min_matches > 0 else ""
        params = {"game": game, "sys": system, "r": rating, "mm": min_matches}

        # Count how many players are rated higher
        rank_row = self.db.client.execute(
            f"""
            SELECT count()
            FROM player_ratings FINAL
            WHERE game_name = %(game)s AND rating_system = %(sys)s AND rating > %(r)s
            {mm_clause}
            """,
            params,
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
            "name": player_name,
            "rank": rank,
            "total": total,
            "rating": round(rating, 1),
            "wins": wins,
            "losses": losses,
            "matches": matches,
            "system": system,
            "game": game or "Combined",
        }

    def get_stats(self) -> dict:
        """Overall system stats."""
        total_matches = self.db.client.execute("SELECT count() FROM matches FINAL")[0][0]
        total_players = self.db.client.execute("SELECT count() FROM players FINAL")[0][0]
        total_downloaded = self.db.client.execute(
            "SELECT count() FROM match_registry FINAL WHERE status = 'parsed'"
        )[0][0]
        total_discovered = self.db.client.execute("SELECT count() FROM match_registry FINAL")[0][0]
        games = self.get_games()

        # Date range from matches
        date_range = self.db.client.execute(
            "SELECT min(played_at), max(played_at) FROM matches"
        )[0]

        # Matches and tournaments per game
        per_game = self.db.client.execute(
            """
            SELECT
                game_name,
                count() AS matches,
                count(DISTINCT tournament_id) AS tournaments
            FROM matches
            GROUP BY game_name
            ORDER BY matches DESC
            """
        )
        matches_per_game = {name: matches for name, matches, _ in per_game}
        tournaments_per_game = {name: tcnt for name, _, tcnt in per_game}

        # Players per game (distinct players who played each game)
        players_per_game = {}
        for row in self.db.client.execute(
            """
            SELECT game_name, count(DISTINCT player_id) AS players FROM (
                SELECT game_name, player1_id AS player_id FROM matches
                UNION ALL
                SELECT game_name, player2_id AS player_id FROM matches
            ) GROUP BY game_name
            """
        ):
            players_per_game[row[0]] = row[1]

        # Active players (played in last 30 days)
        active_players = self.db.client.execute(
            "SELECT count(DISTINCT player_id) FROM ("
            "SELECT player1_id AS player_id FROM matches WHERE played_at >= now() - INTERVAL 30 DAY "
            "UNION ALL "
            "SELECT player2_id AS player_id FROM matches WHERE played_at >= now() - INTERVAL 30 DAY"
            ")"
        )[0][0]

        # Avg matches per player
        avg_matches = round(total_matches / total_players, 1) if total_players > 0 else 0

        # Tournaments
        tournaments = self.db.client.execute(
            "SELECT count(DISTINCT tournament_id) FROM matches"
        )[0][0]

        # Countries
        countries = self.db.client.execute(
            "SELECT count(DISTINCT player1_country) FROM matches WHERE player1_country != ''"
        )[0][0]

        return {
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

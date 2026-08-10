"""ClickHouse database client for Arena Rankings System."""

import logging
from datetime import datetime
from typing import Optional

from clickhouse_driver import Client

from config import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
)

from src.db_schema import DDL_STATEMENTS

logger = logging.getLogger(__name__)


class _DatetimeEpoch:
    """Sentinel for a missing/empty datetime (1970-01-01)."""

    pass


def _coerce_datetime(value):
    """Coerce a value to a datetime for a DateTime column.

    Accepts None (→ epoch), a datetime, or an ISO/"YYYY-MM-DD HH:MM:SS" string.
    Strings like '2026-08-09' (date only) become midnight that day.
    """
    if value is None:
        return datetime(1970, 1, 1)
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        s = value.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    return datetime(1970, 1, 1)


class Database:
    """ClickHouse database client."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        database: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.host = host or CLICKHOUSE_HOST
        self.port = port or CLICKHOUSE_PORT
        self.database = database or CLICKHOUSE_DATABASE
        self.user = user or CLICKHOUSE_USER
        self.password = password or CLICKHOUSE_PASSWORD

        # Ensure the database exists (connect to default first)
        default_client = Client(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
        )
        default_client.execute(
            f"CREATE DATABASE IF NOT EXISTS {self.database}"
        )
        default_client.disconnect()

        # Connect to the target database
        self.client = Client(
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user,
            password=self.password,
        )
        self._init_schema()

    def _init_schema(self):
        """Create tables if they don't exist."""
        for ddl in DDL_STATEMENTS:
            resolved = ddl.strip().replace("arena_rankings.", f"{self.database}.")
            self.client.execute(resolved)

    def close(self):
        """Disconnect from ClickHouse."""
        try:
            self.client.disconnect()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # --- Match Registry ---

    def register_matches(self, match_ids: list[int], played_at_timestamps: Optional[list] = None) -> int:
        """Insert newly discovered match IDs into registry. Skips already-known IDs.

        Args:
            match_ids: List of match IDs.
            played_at_timestamps: Optional list of datetime values (same length as match_ids).
                Timestamp from the matchlist results page — when the match was played.

        Returns:
            Number of newly registered matches.
        """
        if not match_ids:
            return 0

        rows = self.client.execute(
            "SELECT match_id FROM match_registry FINAL WHERE match_id IN %(ids)s",
            {"ids": tuple(match_ids)},
        )
        existing = {r[0] for r in rows}

        new_indices = [i for i, mid in enumerate(match_ids) if mid not in existing]
        if not new_indices:
            return 0

        zero_ts = datetime(1970, 1, 1)
        data = []
        for i in new_indices:
            mid = match_ids[i]
            ts = played_at_timestamps[i] if played_at_timestamps and i < len(played_at_timestamps) else zero_ts
            data.append((mid, ts, "", "discovered"))
        self.client.execute(
            "INSERT INTO match_registry "
            "(match_id, played_at, raw_html, status) VALUES",
            data,
        )
        return len(new_indices)

    def registry_match_exists(self, match_id: int) -> bool:
        """Check if a match_id is already in the registry."""
        rows = self.client.execute(
            "SELECT 1 FROM match_registry WHERE match_id = %(mid)s LIMIT 1",
            {"mid": match_id},
        )
        return len(rows) > 0

    def registry_get_highest_match_id(self) -> int:
        """Get the highest match_id in the registry (0 if empty)."""
        rows = self.client.execute("SELECT max(match_id) FROM match_registry FINAL")
        return rows[0][0] if rows and rows[0][0] else 0

    def registry_count_total(self) -> int:
        """Total matches in registry."""
        rows = self.client.execute("SELECT count() FROM match_registry FINAL")
        return rows[0][0] if rows else 0

    def registry_get_all_ids(self) -> set[int]:
        """Get all known match IDs from the registry."""
        rows = self.client.execute("SELECT match_id FROM match_registry FINAL")
        return {r[0] for r in rows}

    # --- Discovery State ---

    def get_discovery_state(self, key: str, default: str = "") -> str:
        """Get a discovery state value by key (e.g. 'last_known_page')."""
        rows = self.client.execute(
            "SELECT value FROM discovery_state FINAL WHERE key = %(k)s LIMIT 1",
            {"k": key},
        )
        return rows[0][0] if rows else default

    def set_discovery_state(self, key: str, value: str):
        """Set a discovery state value."""
        self.client.execute(
            "INSERT INTO discovery_state (key, value) VALUES",
            [(key, str(value))],
        )

    def get_last_known_page(self) -> int:
        """Get the last known page from discovery state (0 if not set)."""
        val = self.get_discovery_state("last_known_page", "0")
        return int(val)

    def set_last_known_page(self, page: int):
        """Update the last known page in discovery state."""
        self.set_discovery_state("last_known_page", str(page))

    # --- Players ---

    def upsert_player(self, player_id: int, name: str, country: str = ""):
        """Insert or update a player record."""
        self.client.execute(
            "INSERT INTO players "
            "(player_id, name, country) VALUES",
            [(player_id, name, country)],
        )

    # --- Tournaments ---

    def upsert_tournament(
        self,
        tournament_id: int,
        name: str,
        tier: str = "",
        raw_html: str = "",
        game: str = "",
        prize_money: str = "",
        tourney_format: str = "",
        match_format: str = "",
        schedule_start=None,
        schedule_end=None,
        maplist: list = None,
        rankings: str = "[]",
    ):
        """Insert or update a tournament record, including parsed metadata.

        All columns live in the single tournaments table. The parsed fields
        (game, prize, formats, maplist, rankings) are extracted from the
        tournament page HTML by the TournamentResolver; raw_html is the cached
        page source.
        """
        schedule_start = _coerce_datetime(schedule_start)
        schedule_end = _coerce_datetime(schedule_end)
        self.client.execute(
            "INSERT INTO tournaments "
            "(tournament_id, name, tier, raw_html, game, prize_money, "
            " tourney_format, match_format, schedule_start, schedule_end, "
            " maplist, rankings) VALUES",
            [(
                tournament_id, name, tier, raw_html, game, prize_money,
                tourney_format, match_format, schedule_start, schedule_end,
                maplist or [], rankings,
            )],
        )

    def get_tournament_html(self, tournament_id: int) -> str:
        """Return the cached raw_html for a tournament, or empty string if not stored."""
        rows = self.client.execute(
            "SELECT raw_html FROM tournaments FINAL WHERE tournament_id = %(t)s",
            {"t": tournament_id},
        )
        return rows[0][0] if rows else ""

    def get_tournament_details(self, tournament_id: int) -> dict:
        """Return parsed tournament metadata dict, or None if not stored."""
        rows = self.client.execute(
            "SELECT tournament_id, name, tier, game, prize_money, tourney_format, "
            " match_format, schedule_start, schedule_end, maplist, rankings "
            "FROM tournaments FINAL WHERE tournament_id = %(t)s",
            {"t": tournament_id},
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "tournament_id": r[0],
            "name": r[1],
            "tier": r[2],
            "game": r[3],
            "prize_money": r[4],
            "tourney_format": r[5],
            "match_format": r[6],
            "schedule_start": r[7],
            "schedule_end": r[8],
            "maplist": r[9],
            "rankings": r[10],
        }

    # --- Matches ---

    def insert_match(self, detail) -> bool:
        """Insert a parsed match detail into the matches table.

        Args:
            detail: MatchDetail dataclass instance.

        Returns:
            True if inserted.
        """
        self.client.execute(
            "INSERT INTO matches "
            "(match_id, player1_id, player2_id, player1_name, player2_name, "
            "player1_country, player2_country, player1_score, player2_score, winner_id, "
            "game_name, game_category_id, match_format, tournament_id, tournament_name, "
            "stage_name, played_at, status) VALUES",
            [(
                detail.match_id,
                detail.player1_id, detail.player2_id,
                detail.player1_name, detail.player2_name,
                detail.player1_country, detail.player2_country,
                detail.player1_score, detail.player2_score,
                detail.winner_id,
                detail.game_name, detail.game_category_id,
                detail.match_format, detail.tournament_id,
                detail.tournament_name, detail.stage_name,
                detail.played_at, detail.status,
            )],
        )
        return True

    # --- Match Maps ---

    def insert_match_maps(self, match_id: int, maps: list, played_at: datetime):
        """Insert map results for a match.

        Args:
            match_id: PlusForward match ID.
            maps: List of MapResult dataclass instances.
            played_at: When the match was played.
        """
        if not maps:
            return
        data = [
            (
                match_id, i, m.map_name,
                m.player1_name, m.player2_name,
                m.player1_score, m.player2_score,
                played_at,
            )
            for i, m in enumerate(maps)
        ]
        self.client.execute(
            "INSERT INTO match_maps "
            "(match_id, map_index, map_name, player1_name, player2_name, "
            "player1_score, player2_score, played_at) VALUES",
            data,
        )

    # --- Player Ratings ---

    def upsert_rating(
        self,
        player_id: int,
        player_name: str,
        game_name: str,
        rating_system: str,
        rating: float,
        rd: float = 350.0,
        vol: float = 0.06,
        wins: int = 0,
        losses: int = 0,
        matches_played: int = 0,
        last_match_id: int = 0,
        last_match_date: datetime = None,
        first_match_date: datetime = None,
    ):
        """Insert or update a player rating record."""
        if last_match_date is None:
            last_match_date = datetime(1970, 1, 1)
        if first_match_date is None:
            first_match_date = datetime(1970, 1, 1)
        self.client.execute(
            "INSERT INTO player_ratings "
            "(player_id, player_name, game_name, rating_system, rating, rd, vol, "
            "wins, losses, matches_played, last_match_id, last_match_date, first_match_date) VALUES",
            [(
                player_id, player_name, game_name, rating_system,
                rating, rd, vol, wins, losses, matches_played,
                last_match_id, last_match_date, first_match_date,
            )],
        )

    def upsert_ratings_batch(self, rows: list[tuple]):
        """Batch-insert player rating records.

        Args:
            rows: List of tuples matching the player_ratings column order
                  (player_id, player_name, game_name, rating_system, rating, rd, vol,
                   wins, losses, matches_played, last_match_id, last_match_date, first_match_date)
        """
        if not rows:
            return
        self.client.execute(
            "INSERT INTO player_ratings "
            "(player_id, player_name, game_name, rating_system, rating, rd, vol, "
            "wins, losses, matches_played, last_match_id, last_match_date, first_match_date) VALUES",
            rows,
        )

    def clear_ratings(self, rating_system: str = "", game_name: str = ""):
        """Clear stored ratings. If rating_system/game_name given, only clear matching rows.

        Note: ClickHouse doesn't support DELETE on ReplacingMergeTree directly in all versions,
        so we use ALTER TABLE DELETE which is asynchronous but works with MergeTree family.
        """
        conditions = []
        params = {}
        if rating_system:
            conditions.append("rating_system = %(rs)s")
            params["rs"] = rating_system
        if game_name is not None:
            conditions.append("game_name = %(gn)s")
            params["gn"] = game_name
        where = " AND ".join(conditions) if conditions else "1=1"
        self.client.execute(
            f"ALTER TABLE player_ratings DELETE WHERE {where}",
            params,
        )

    def clear_rating_history(self, rating_system: str = "", game_name: str = ""):
        """Clear rating history rows. If rating_system/game_name given, only clear matching."""
        conditions = []
        params = {}
        if rating_system:
            conditions.append("rating_system = %(rs)s")
            params["rs"] = rating_system
        if game_name is not None:
            conditions.append("game_name = %(gn)s")
            params["gn"] = game_name
        where = " AND ".join(conditions) if conditions else "1=1"
        self.client.execute(
            f"ALTER TABLE rating_history DELETE WHERE {where}",
            params,
        )

    def insert_rating_history_batch(self, rows: list[tuple]):
        """Batch-insert rating history snapshots.

        Args:
            rows: List of tuples matching rating_history column order
                  (player_id, game_name, rating_system, match_id, played_at,
                   rating, rd, vol, wins, losses, matches_played)
        """
        if not rows:
            return
        self.client.execute(
            "INSERT INTO rating_history "
            "(player_id, game_name, rating_system, match_id, played_at, "
            "rating, rd, vol, wins, losses, matches_played) VALUES",
            rows,
        )

    def count_matches_for_game(self, game_name: str = "") -> int:
        """Count parsed duel matches for a specific game (or all games if empty)."""
        duel_filter = (
            "((match_format ILIKE '%%duel%%' AND match_format NOT ILIKE '%%team%%')"
            " OR match_format ILIKE '%%1v1%%')"
        )
        if game_name:
            rows = self.client.execute(
                "SELECT count() FROM matches FINAL WHERE game_name = %(g)s AND " + duel_filter,
                {"g": game_name},
            )
        else:
            rows = self.client.execute(
                "SELECT count() FROM matches FINAL WHERE " + duel_filter
            )
        return rows[0][0] if rows else 0

    def count_rating_history(self, game_name: str, rating_system: str) -> int:
        """Count distinct match_ids in rating_history for a game/system."""
        if game_name:
            rows = self.client.execute(
                "SELECT count(DISTINCT match_id) FROM rating_history "
                "WHERE game_name = %(g)s AND rating_system = %(rs)s",
                {"g": game_name, "rs": rating_system},
            )
        else:
            rows = self.client.execute(
                "SELECT count(DISTINCT match_id) FROM rating_history WHERE rating_system = %(rs)s",
                {"rs": rating_system},
            )
        return rows[0][0] if rows else 0

    def get_all_matches_for_game(self, game_name: str = "") -> list:
        """Get all parsed matches for a specific game (or all games if empty).

        Only returns 1v1 duel matches.
        """
        duel_filter = (
            "((match_format ILIKE '%%duel%%' AND match_format NOT ILIKE '%%team%%')"
            " OR match_format ILIKE '%%1v1%%')"
        )
        if game_name:
            return self.client.execute(
                "SELECT match_id, player1_id, player2_id, player1_score, player2_score, "
                "winner_id, played_at, game_name, tournament_id "
                "FROM matches FINAL WHERE game_name = %(g)s AND " + duel_filter +
                " ORDER BY played_at",
                {"g": game_name},
            )
        return self.client.execute(
            "SELECT match_id, player1_id, player2_id, player1_score, player2_score, "
            "winner_id, played_at, game_name, tournament_id "
            "FROM matches FINAL WHERE " + duel_filter + " ORDER BY played_at"
        )

    def get_matches_for_game_after(self, game_name: str = "", after_match_id: int = 0) -> list:
        """Get matches with match_id > after_match_id for a game (or all games if empty).

        Only returns 1v1 duel matches (excludes team formats like TDM, CTF, Team 6v6).
        """
        duel_filter = (
            "((match_format ILIKE '%%duel%%' AND match_format NOT ILIKE '%%team%%')"
            " OR match_format ILIKE '%%1v1%%')"
        )
        if game_name:
            return self.client.execute(
                "SELECT match_id, player1_id, player2_id, player1_score, player2_score, "
                "winner_id, played_at, game_name, tournament_id "
                "FROM matches FINAL "
                "WHERE game_name = %(g)s AND match_id > %(mid)s "
                "AND " + duel_filter + " "
                "ORDER BY played_at",
                {"g": game_name, "mid": after_match_id},
            )
        return self.client.execute(
            "SELECT match_id, player1_id, player2_id, player1_score, player2_score, "
            "winner_id, played_at, game_name, tournament_id "
            "FROM matches FINAL WHERE match_id > %(mid)s "
            "AND " + duel_filter + " "
            "ORDER BY played_at",
            {"mid": after_match_id},
        )

    def get_last_processed_match_id(self, game_name: str, rating_system: str) -> int:
        """Get the last match_id processed in rating_history for a game/system."""
        rows = self.client.execute(
            "SELECT max(match_id) FROM rating_history "
            "WHERE game_name = %(g)s AND rating_system = %(rs)s",
            {"g": game_name, "rs": rating_system},
        )
        return rows[0][0] if rows and rows[0][0] else 0

    def get_max_match_id(self, game_name: str = "") -> int:
        """Get the highest match_id in the matches table for a game (or all games if empty)."""
        if game_name:
            rows = self.client.execute(
                "SELECT max(match_id) FROM matches FINAL WHERE game_name = %(g)s",
                {"g": game_name},
            )
        else:
            rows = self.client.execute("SELECT max(match_id) FROM matches FINAL")
        return rows[0][0] if rows and rows[0][0] else 0

    def get_max_match_id_in_history(self, game_name: str, rating_system: str) -> int:
        """Get the highest match_id in rating_history for a game/system."""
        rows = self.client.execute(
            "SELECT max(match_id) FROM rating_history "
            "WHERE game_name = %(g)s AND rating_system = %(rs)s",
            {"g": game_name, "rs": rating_system},
        )
        return rows[0][0] if rows and rows[0][0] else 0

    def get_max_played_at(self, game_name: str = "") -> datetime:
        """Get the latest played_at timestamp in the matches table for a game (or all games if empty)."""
        if game_name:
            rows = self.client.execute(
                "SELECT max(played_at) FROM matches FINAL WHERE game_name = %(g)s",
                {"g": game_name},
            )
        else:
            rows = self.client.execute("SELECT max(played_at) FROM matches FINAL")
        return rows[0][0] if rows and rows[0][0] else None

    def get_last_processed_date(self, game_name: str, rating_system: str):
        """Get the last played_at date processed in rating_history for a game/system."""
        rows = self.client.execute(
            "SELECT max(played_at) FROM rating_history "
            "WHERE game_name = %(g)s AND rating_system = %(rs)s",
            {"g": game_name, "rs": rating_system},
        )
        return rows[0][0] if rows and rows[0][0] else None

    def load_ratings(self, game_name: str, rating_system: str) -> dict:
        """Load current ratings from player_ratings for a game/system.

        Returns dict mapping player_id -> {rating, rd, vol, wins, losses,
        matches, name, last_match_id, last_match_date}.
        """
        rows = self.client.execute(
            "SELECT player_id, player_name, rating, rd, vol, wins, losses, "
            "matches_played, last_match_id, last_match_date, first_match_date "
            "FROM player_ratings FINAL "
            "WHERE game_name = %(g)s AND rating_system = %(rs)s",
            {"g": game_name, "rs": rating_system},
        )
        ratings = {}
        for row in rows:
            pid, name, rating, rd, vol, wins, losses, matches, last_mid, last_date, first_date = row
            ratings[pid] = {
                "rating": rating,
                "rd": rd,
                "vol": vol,
                "wins": wins,
                "losses": losses,
                "matches": matches,
                "name": name,
                "last_match_id": last_mid,
                "last_match_date": last_date,
                "first_match_date": first_date,
            }
        return ratings

    def delete_rating_history_from_date(self, game_name: str, rating_system: str, from_date):
        """Delete rating history rows with played_at >= from_date.

        Used for Glicko-2 incremental: clear the last (possibly incomplete) period
        so it can be recomputed with new matches.
        """
        self.client.execute(
            "ALTER TABLE rating_history DELETE "
            "WHERE game_name = %(g)s AND rating_system = %(rs)s AND played_at >= %(d)s",
            {"g": game_name, "rs": rating_system, "d": from_date},
        )

    def get_state_before_date(self, game_name: str, rating_system: str, before_date) -> dict:
        """Get player states (rating, rd, vol, wins, losses, matches) as of the last
        history entry strictly before `before_date`.

        This gives the correct starting state for incremental Glicko-2: the state
        before the last period was processed.
        """
        rows = self.client.execute(
            "SELECT player_id, "
            "argMax(rating, played_at) as rating, "
            "argMax(rd, played_at) as rd, "
            "argMax(vol, played_at) as vol, "
            "argMax(wins, played_at) as wins, "
            "argMax(losses, played_at) as losses, "
            "argMax(matches_played, played_at) as matches "
            "FROM rating_history "
            "WHERE game_name = %(g)s AND rating_system = %(rs)s AND played_at < %(d)s "
            "GROUP BY player_id",
            {"g": game_name, "rs": rating_system, "d": before_date},
        )
        return {r[0]: {"rating": r[1], "rd": r[2], "vol": r[3], "wins": r[4], "losses": r[5], "matches": r[6]} for r in rows}

    def get_stats_before_date(self, game_name: str, rating_system: str, before_date) -> dict:
        """Get cumulative wins/losses/matches per player as of the last history
        entry strictly before `before_date`.

        This gives the correct stats to start incremental Glicko-2 from, avoiding
        the double-subtract problem of rolling back from current ratings.
        """
        rows = self.client.execute(
            "SELECT player_id, argMax(wins, played_at) as wins, "
            "argMax(losses, played_at) as losses, argMax(matches_played, played_at) as matches "
            "FROM rating_history "
            "WHERE game_name = %(g)s AND rating_system = %(rs)s AND played_at < %(d)s "
            "GROUP BY player_id",
            {"g": game_name, "rs": rating_system, "d": before_date},
        )
        return {r[0]: {"wins": r[1], "losses": r[2], "matches": r[3]} for r in rows}

    def get_matches_for_game_from_date(self, game_name: str = "", from_date=None) -> list:
        """Get matches with played_at >= from_date for a game (or all games if empty).

        Only returns 1v1 duel matches.
        """
        duel_filter = (
            "((match_format ILIKE '%%duel%%' AND match_format NOT ILIKE '%%team%%')"
            " OR match_format ILIKE '%%1v1%%')"
        )
        if game_name:
            return self.client.execute(
                "SELECT match_id, player1_id, player2_id, player1_score, player2_score, "
                "winner_id, played_at, game_name, tournament_id "
                "FROM matches FINAL "
                "WHERE game_name = %(g)s AND played_at >= %(d)s "
                "AND " + duel_filter + " "
                "ORDER BY played_at",
                {"g": game_name, "d": from_date},
            )
        return self.client.execute(
            "SELECT match_id, player1_id, player2_id, player1_score, player2_score, "
            "winner_id, played_at, game_name, tournament_id "
            "FROM matches FINAL WHERE played_at >= %(d)s "
            "AND " + duel_filter + " "
            "ORDER BY played_at",
            {"d": from_date},
        )

    def get_tournament_tier(self, tournament_id: int) -> str:
        """Return the stored tier for a single tournament, or '' if not parsed."""
        rows = self.client.execute(
            "SELECT tier FROM tournaments FINAL WHERE tournament_id = %(t)s",
            {"t": tournament_id},
        )
        return rows[0][0] if rows and rows[0][0] else ""

    def get_tournament_tiers(self) -> dict[int, str]:
        """Load all tournament tiers from the tournaments table.

        Returns dict mapping tournament_id -> tier string.
        """
        rows = self.client.execute(
            "SELECT tournament_id, tier FROM tournaments FINAL"
        )
        return {tid: tier for tid, tier in rows}

    def get_tournament_tiers_with_html(self) -> dict[int, str]:
        """Load tournament tiers for tournaments that have raw_html stored.

        Used by the resolver preload — only tournaments with cached HTML
        can skip the network fetch. Tournaments with tier but no raw_html
        still need to be fetched.

        Returns dict mapping tournament_id -> tier string.
        """
        rows = self.client.execute(
            "SELECT tournament_id, tier FROM tournaments FINAL WHERE raw_html != ''"
        )
        return {tid: tier for tid, tier in rows}
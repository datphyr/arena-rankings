"""ClickHouse database client for Arena Rankings System."""

import logging
import json
import threading
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

# The schema (CREATE DATABASE + CREATE TABLE IF NOT EXISTS) only needs to run
# once per process. Every request constructs a fresh Database/DataProvider, and
# re-running the 9 schema round-trips on each one added ~9ms of overhead per
# page. A module-level flag (guarded by a lock) skips the no-op DDL after the
# first successful initialization.
_schema_ready = False
_schema_lock = threading.Lock()

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


def download_complete() -> bool:
    """True once the download stage has scanned to the end of posts (hit the wall).

    Opens its own connection and checks the discovery_state latch. Used by the
    parse/rank pipeline components to gate their first run: they hold off until
    the download stage has catalogued the full post history (reached the
    'Invalid post id.' wall) so processing runs oldest→newest and nothing is
    missed.
    """
    try:
        db = Database()
        try:
            return db.is_download_complete()
        finally:
            db.close()
    except Exception:
        # If we can't check the gate, fail closed (don't start the pipeline
        # on a half-scanned history).
        return False


def discovery_complete() -> bool:
    """True once matchlist discovery has scanned back to the oldest match.

    Opens its own connection and checks the discovery_state latch. Used by the
    download/parse pipeline components in discovery mode to gate their first
    run: they hold off until discovery has catalogued the full match history
    (oldest match found) so processing runs oldest→newest and nothing is missed.
    """
    try:
        db = Database()
        try:
            return db.is_backward_complete()
        finally:
            db.close()
    except Exception:
        # If we can't check the gate, fail closed (don't start the pipeline
        # on a half-scanned history).
        return False


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

        # Ensure the database exists (connect to default first). Only needed
        # once per process — after the first init the database is guaranteed to
        # exist, so skip the extra default-client round-trip on later requests.
        global _schema_ready
        if not _schema_ready:
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
        # Run the CREATE TABLE IF NOT EXISTS DDL only once per process (see
        # module docstring). The tables already exist after the first init, so
        # skipping the no-op round-trips on every request is safe.
        if not _schema_ready:
            with _schema_lock:
                if not _schema_ready:
                    self._init_schema()
                    _schema_ready = True

    def _init_schema(self):
        """Create tables if they don't exist."""
        for ddl in DDL_STATEMENTS:
            resolved = ddl.strip().replace("arena_rankings.", f"{self.database}.")
            self.client.execute(resolved)

    # --- Game ID resolution ---

    def _game_id_cache(self) -> dict:
        """Lazily load the games table into a name -> game_id map."""
        if not hasattr(self, "_game_ids"):
            rows = self.client.execute("SELECT game_id, name FROM games FINAL")
            self._game_ids = {name: gid for gid, name in rows}
        return self._game_ids

    def resolve_game_id(self, game_name: str) -> int:
        """Resolve a game display name to its game_id (PlusForward category ID).

        Returns 0 if the game is unknown or game_name is empty (all games).
        """
        if not game_name:
            return 0
        return self._game_id_cache().get(game_name, 0)

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

    # --- Raw Posts ---

    def store_raw_post(self, post_id: int, raw_html: str, status: str = "downloaded",
                       reason: str = "", sort_time=None):
        """Insert or update a raw post (downloaded HTML + processing status).

        ReplacingMergeTree dedups by post_id, so re-downloading a post
        overwrites its previous row.

        sort_time is the chronological ordering key (see schema). Pass None to
        keep the existing value (e.g. when only flipping status via raw_post_mark).
        """
        if sort_time is None:
            # Preserve the existing sort_time if the row already exists.
            rows = self.client.execute(
                "SELECT sort_time FROM raw_posts FINAL WHERE post_id = %(pid)s LIMIT 1",
                {"pid": post_id},
            )
            sort_time = rows[0][0] if rows else datetime(1970, 1, 1)
        self.client.execute(
            "INSERT INTO raw_posts (post_id, raw_html, status, reason, sort_time) VALUES",
            [(post_id, raw_html, status, reason, sort_time)],
        )

    def store_raw_posts(self, posts: list[tuple]):
        """Batch-insert raw posts. Each tuple: (post_id, raw_html, status, reason, sort_time)."""
        if not posts:
            return
        self.client.execute(
            "INSERT INTO raw_posts (post_id, raw_html, status, reason, sort_time) VALUES",
            posts,
        )

    def raw_post_exists(self, post_id: int) -> bool:
        """Check if a post_id is already in raw_posts."""
        rows = self.client.execute(
            "SELECT 1 FROM raw_posts WHERE post_id = %(pid)s LIMIT 1",
            {"pid": post_id},
        )
        return len(rows) > 0

    def raw_post_get(self, post_id: int) -> Optional[tuple]:
        """Return (post_id, raw_html, status, reason, sort_time) for a post, or None."""
        rows = self.client.execute(
            "SELECT post_id, raw_html, status, reason, sort_time FROM raw_posts FINAL WHERE post_id = %(pid)s",
            {"pid": post_id},
        )
        return rows[0] if rows else None

    def raw_post_get_html(self, post_id: int) -> str:
        """Return the raw_html for a post, or empty string if not stored."""
        rows = self.client.execute(
            "SELECT raw_html FROM raw_posts FINAL WHERE post_id = %(pid)s",
            {"pid": post_id},
        )
        return rows[0][0] if rows else ""

    def raw_post_get_highest_id(self) -> int:
        """Get the highest post_id in raw_posts (0 if empty)."""
        rows = self.client.execute("SELECT max(post_id) FROM raw_posts FINAL")
        return rows[0][0] if rows and rows[0][0] else 0

    def raw_post_count_total(self) -> int:
        """Total posts in raw_posts."""
        rows = self.client.execute("SELECT count() FROM raw_posts FINAL")
        return rows[0][0] if rows else 0

    def raw_post_get_all_ids(self) -> set[int]:
        """Get all known post IDs from raw_posts."""
        rows = self.client.execute("SELECT post_id FROM raw_posts FINAL")
        return {r[0] for r in rows}

    def raw_post_get_downloaded(self, limit: int = 0) -> list[tuple]:
        """Get posts with status 'downloaded' (ready to parse), chronological first.

        Returns list of (post_id, raw_html) tuples, ordered by sort_time (the
        reliable chronological key — post_id is NOT chronological).
        """
        query = (
            "SELECT post_id, raw_html FROM raw_posts FINAL "
            "WHERE status = 'downloaded' ORDER BY sort_time ASC, post_id ASC"
        )
        if limit > 0:
            query += f" LIMIT {limit}"
        rows = self.client.execute(query)
        return [(r[0], r[1]) for r in rows]

    def raw_post_mark(self, post_id: int, status: str, reason: str = ""):
        """Update a post's status (and optional reason), preserving its HTML + sort_time."""
        html = self.raw_post_get_html(post_id)
        self.store_raw_post(post_id, html, status, reason)

    # --- Discovery catalog (discovery download mode) ---
    #
    # In discovery mode, matchlist discovery registers match IDs in raw_posts
    # with status 'discovered' (raw_html empty, sort_time from the matchlist).
    # The downloader then fetches each and flips it to 'downloaded'. This keeps
    # raw_posts as the single source of truth — no separate match_registry table.

    def register_discovered(self, match_ids: list[int], sort_times: Optional[list] = None) -> int:
        """Insert newly discovered match IDs into raw_posts as status 'discovered'.

        Skips IDs already present (any status). Returns the number newly added.
        sort_times: optional list of datetime values (same length as match_ids),
        from the matchlist results page — when the match was played.
        """
        if not match_ids:
            return 0
        rows = self.client.execute(
            "SELECT post_id FROM raw_posts FINAL WHERE post_id IN %(ids)s",
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
            ts = sort_times[i] if sort_times and i < len(sort_times) else zero_ts
            data.append((mid, "", "discovered", "", ts))
        self.client.execute(
            "INSERT INTO raw_posts (post_id, raw_html, status, reason, sort_time) VALUES",
            data,
        )
        return len(new_indices)

    def raw_post_get_discovered(self, limit: int = 0) -> list[tuple]:
        """Get posts with status 'discovered' (match IDs awaiting download), chronological first.

        Returns list of (post_id, sort_time) tuples.
        """
        query = (
            "SELECT post_id, sort_time FROM raw_posts FINAL "
            "WHERE status = 'discovered' ORDER BY sort_time ASC, post_id ASC"
        )
        if limit > 0:
            query += f" LIMIT {limit}"
        return self.client.execute(query)

    def raw_post_count_discovered(self) -> int:
        """Count posts with status 'discovered' (awaiting download)."""
        rows = self.client.execute(
            "SELECT count() FROM raw_posts FINAL WHERE status = 'discovered'"
        )
        return rows[0][0] if rows else 0

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

    def is_download_complete(self) -> bool:
        """True once the download stage has hit the post wall (scanned past the
        last valid post). Latches to True forever once reached.
        """
        return self.get_discovery_state("download_complete", "0") == "1"

    def is_backward_complete(self) -> bool:
        """True once matchlist discovery has scanned back to the oldest match.

        Used as the gate for the download/parse pipeline in discovery mode: they
        wait until discovery has catalogued the full match history (oldest match
        found) before starting to process, so ratings are computed chronologically
        and nothing is missed. Latches to True forever once reached.
        """
        return self.get_discovery_state("backward_complete", "0") == "1"

    def get_last_known_page(self) -> int:
        """Get the last known matchlist page from discovery state (0 if not set)."""
        val = self.get_discovery_state("last_known_page", "0")
        return int(val)

    def set_last_known_page(self, page: int):
        """Update the last known matchlist page in discovery state."""
        self.set_discovery_state("last_known_page", str(page))

    def get_last_scanned_post(self) -> int:
        """Get the last post_id scanned by the sequential download stage (0 if not set)."""
        val = self.get_discovery_state("last_scanned_post", "0")
        return int(val)

    def set_last_scanned_post(self, post_id: int):
        """Update the last scanned post id in discovery state."""
        self.set_discovery_state("last_scanned_post", str(post_id))

    # --- Players ---

    def upsert_player(self, player_id: int, name: str, country: str = ""):
        """Insert or update a player record."""
        self.client.execute(
            "INSERT INTO players "
            "(player_id, name, country) VALUES",
            [(player_id, name, country)],
        )

    # --- Games ---

    def upsert_game(self, game_id: int, name: str):
        """Insert or update a game record (game_id = PlusForward category ID)."""
        if game_id <= 0:
            return
        self.client.execute(
            "INSERT INTO games "
            "(game_id, name) VALUES",
            [(game_id, name)],
        )

    # --- Maps ---

    def upsert_map(self, map_id: int, name: str, image: str = "", game: str = ""):
        """Insert or update a map record (map_id -> name/image/game).

        Populated by the pipeline from parsed match map data (ReplacingMergeTree
        dedups by map_id, so re-parsing the same map just overwrites metadata).
        The maps table is the canonical source for map names/images used by the
        player map chart and tournament map plaques.
        """
        if map_id <= 0 or not name:
            return
        self.client.execute(
            "INSERT INTO maps "
            "(map_id, name, image, game) VALUES",
            [(map_id, name, image, game)],
        )

    # --- Player aliases ---

    def record_aliases(self, player_id: int, names: list[str]):
        """Record historical name spellings for a player (one row per spelling).

        Each call increments the count for each distinct spelling, so the count
        reflects how many matches used that spelling. ReplacingMergeTree dedups
        by (player_id, name); we insert the accumulated count (existing + 1)
        so re-parsing the same spelling bumps the count instead of resetting it.
        """
        if not names:
            return
        for n in names:
            if not n:
                continue
            # Increment the count for this spelling (existing count + 1).
            self.client.execute(
                "INSERT INTO player_aliases (player_id, name, count) "
                "SELECT %(pid)s, %(name)s, ifNull(max(count), 0) + 1 "
                "FROM player_aliases FINAL WHERE player_id = %(pid)s AND name = %(name)s",
                {"pid": player_id, "name": n},
            )

    # --- Tournaments ---

    def upsert_tournament(
        self,
        tournament_id: int,
        name: str,
        tier: str = "",
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
        tournament page HTML by the TournamentResolver. The raw page HTML
        itself lives in raw_posts (post_id = tournament_id).
        """
        schedule_start = _coerce_datetime(schedule_start)
        schedule_end = _coerce_datetime(schedule_end)
        self.client.execute(
            "INSERT INTO tournaments "
            "(tournament_id, name, tier, game, prize_money, "
            " tourney_format, match_format, schedule_start, schedule_end, "
            " maplist, rankings) VALUES",
            [(
                tournament_id, name, tier, game, prize_money,
                tourney_format, match_format, schedule_start, schedule_end,
                maplist or [], rankings,
            )],
        )

    def get_tournament_html(self, tournament_id: int) -> str:
        """Return the cached raw_html for a tournament, or empty string if not stored.

        The HTML lives in raw_posts (post_id = tournament_id).
        """
        return self.raw_post_get_html(tournament_id)

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

    # --- Tournament brackets ---

    def upsert_tournament_bracket(self, tournament_id: int, source: str, data: str):
        """Store (or overwrite) the cached bracket data for a tournament."""
        self.client.execute(
            "INSERT INTO tournament_brackets "
            "(tournament_id, source, data, fetched_at) VALUES",
            [(tournament_id, source, data, datetime.utcnow())],
        )

    def get_tournament_bracket(self, tournament_id: int) -> dict | None:
        """Return the cached bracket dict, or None if not stored.

        Returns {"source": ..., "data": <parsed JSON dict>, "fetched_at": ...}.
        """
        rows = self.client.execute(
            "SELECT source, data, fetched_at FROM tournament_brackets FINAL "
            "WHERE tournament_id = %(t)s",
            {"t": tournament_id},
        )
        if not rows:
            return None
        source, data, fetched_at = rows[0]
        try:
            parsed = json.loads(data or "{}")
        except Exception:
            parsed = {}
        return {"source": source, "data": parsed, "fetched_at": fetched_at}

    # --- Matches ---

    def insert_match(self, detail) -> bool:
        """Insert a parsed match detail into the matches table (IDs only).

        Args:
            detail: MatchDetail dataclass instance.

        Returns:
            True if inserted.
        """
        self.client.execute(
            "INSERT INTO matches "
            "(match_id, player1_id, player2_id, "
            "player1_score, player2_score, winner_id, "
            "game_id, match_format, tournament_id, "
            "stage_name, played_at, "
            "player1_country, player2_country) VALUES",
            [(
                detail.match_id,
                detail.player1_id, detail.player2_id,
                detail.player1_score, detail.player2_score,
                detail.winner_id,
                detail.game_category_id,
                detail.match_format, detail.tournament_id,
                detail.stage_name,
                detail.played_at,
                detail.player1_country, detail.player2_country,
            )],
        )
        return True

    # --- Match Maps ---

    def insert_match_maps(self, match_id: int, maps: list, played_at: datetime):
        """Insert map results for a match (IDs only).

        Args:
            match_id: PlusForward match ID.
            maps: List of MapResult dataclass instances.
            played_at: When the match was played.
        """
        if not maps:
            return
        data = [
            (
                match_id, i, m.map_id,
                m.player1_score, m.player2_score,
                played_at,
            )
            for i, m in enumerate(maps)
        ]
        self.client.execute(
            "INSERT INTO match_maps "
            "(match_id, map_index, map_id, "
            "player1_score, player2_score, played_at) VALUES",
            data,
        )

    # --- Player Ratings ---

    def upsert_rating(
        self,
        player_id: int,
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
        gid = self.resolve_game_id(game_name)
        self.client.execute(
            "INSERT INTO player_ratings "
            "(player_id, game_id, rating_system, rating, rd, vol, "
            "wins, losses, matches_played, last_match_id, last_match_date, first_match_date) VALUES",
            [(
                player_id, gid, rating_system,
                rating, rd, vol, wins, losses, matches_played,
                last_match_id, last_match_date, first_match_date,
            )],
        )

    def upsert_ratings_batch(self, rows: list[tuple]):
        """Batch-insert player rating records.

        Args:
            rows: List of tuples matching the player_ratings column order
                  (player_id, game_id, rating_system, rating, rd, vol,
                   wins, losses, matches_played, last_match_id, last_match_date, first_match_date)
        """
        if not rows:
            return
        self.client.execute(
            "INSERT INTO player_ratings "
            "(player_id, game_id, rating_system, rating, rd, vol, "
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
            conditions.append("game_id = %(gid)s")
            params["gid"] = self.resolve_game_id(game_name)
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
            conditions.append("game_id = %(gid)s")
            params["gid"] = self.resolve_game_id(game_name)
        where = " AND ".join(conditions) if conditions else "1=1"
        self.client.execute(
            f"ALTER TABLE rating_history DELETE WHERE {where}",
            params,
        )

    def insert_rating_history_batch(self, rows: list[tuple]):
        """Batch-insert rating history snapshots.

        Args:
            rows: List of tuples matching rating_history column order
                  (player_id, game_id, rating_system, match_id, played_at,
                   rating, rd, vol, wins, losses, matches_played)
        """
        if not rows:
            return
        self.client.execute(
            "INSERT INTO rating_history "
            "(player_id, game_id, rating_system, match_id, played_at, "
            "rating, rd, vol, wins, losses, matches_played) VALUES",
            rows,
        )

    def count_matches_for_game(self, game_name: str = "") -> int:
        """Count parsed duel matches for a specific game (or all games if empty)."""
        duel_filter = (
            "((match_format ILIKE '%%duel%%' AND match_format NOT ILIKE '%%team%%')"
            " OR match_format ILIKE '%%1v1%%')"
        )
        gid = self.resolve_game_id(game_name)
        if gid:
            rows = self.client.execute(
                "SELECT count() FROM matches FINAL WHERE game_id = %(g)s AND " + duel_filter,
                {"g": gid},
            )
        else:
            rows = self.client.execute(
                "SELECT count() FROM matches FINAL WHERE " + duel_filter
            )
        return rows[0][0] if rows else 0

    def count_rating_history(self, game_name: str, rating_system: str) -> int:
        """Count distinct match_ids in rating_history for a game/system."""
        if game_name:
            gid = self.resolve_game_id(game_name)
            rows = self.client.execute(
                "SELECT count(DISTINCT match_id) FROM rating_history "
                "WHERE game_id = %(g)s AND rating_system = %(rs)s",
                {"g": gid, "rs": rating_system},
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
        gid = self.resolve_game_id(game_name)
        if gid:
            return self.client.execute(
                "SELECT match_id, player1_id, player2_id, player1_score, player2_score, "
                "winner_id, played_at, game_id, tournament_id "
                "FROM matches FINAL WHERE game_id = %(g)s AND " + duel_filter +
                " ORDER BY played_at",
                {"g": gid},
            )
        return self.client.execute(
            "SELECT match_id, player1_id, player2_id, player1_score, player2_score, "
            "winner_id, played_at, game_id, tournament_id "
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
        gid = self.resolve_game_id(game_name)
        if gid:
            return self.client.execute(
                "SELECT match_id, player1_id, player2_id, player1_score, player2_score, "
                "winner_id, played_at, game_id, tournament_id "
                "FROM matches FINAL "
                "WHERE game_id = %(g)s AND match_id > %(mid)s "
                "AND " + duel_filter + " "
                "ORDER BY played_at",
                {"g": gid, "mid": after_match_id},
            )
        return self.client.execute(
            "SELECT match_id, player1_id, player2_id, player1_score, player2_score, "
            "winner_id, played_at, game_id, tournament_id "
            "FROM matches FINAL WHERE match_id > %(mid)s "
            "AND " + duel_filter + " "
            "ORDER BY played_at",
            {"mid": after_match_id},
        )

    def get_last_processed_match_id(self, game_name: str, rating_system: str) -> int:
        """Get the last match_id processed in rating_history for a game/system."""
        gid = self.resolve_game_id(game_name)
        rows = self.client.execute(
            "SELECT max(match_id) FROM rating_history "
            "WHERE game_id = %(g)s AND rating_system = %(rs)s",
            {"g": gid, "rs": rating_system},
        )
        return rows[0][0] if rows and rows[0][0] else 0

    def get_last_processed_match_time(self, game_name: str, rating_system: str):
        """Get the max played_at processed in rating_history for a game/system.

        Used to detect out-of-order match arrivals (a newly parsed match played
        earlier than what's already rated) so the incremental rating compute can
        fall back to a full recompute instead of corrupting history.
        """
        gid = self.resolve_game_id(game_name)
        rows = self.client.execute(
            "SELECT max(played_at) FROM rating_history "
            "WHERE game_id = %(g)s AND rating_system = %(rs)s",
            {"g": gid, "rs": rating_system},
        )
        return rows[0][0] if rows else None

    def get_max_match_id(self, game_name: str = "") -> int:
        """Get the highest match_id in the matches table for a game (or all games if empty)."""
        gid = self.resolve_game_id(game_name)
        if gid:
            rows = self.client.execute(
                "SELECT max(match_id) FROM matches FINAL WHERE game_id = %(g)s",
                {"g": gid},
            )
        else:
            rows = self.client.execute("SELECT max(match_id) FROM matches FINAL")
        return rows[0][0] if rows and rows[0][0] else 0

    def get_max_match_id_in_history(self, game_name: str, rating_system: str) -> int:
        """Get the highest match_id in rating_history for a game/system."""
        gid = self.resolve_game_id(game_name)
        rows = self.client.execute(
            "SELECT max(match_id) FROM rating_history "
            "WHERE game_id = %(g)s AND rating_system = %(rs)s",
            {"g": gid, "rs": rating_system},
        )
        return rows[0][0] if rows and rows[0][0] else 0

    def get_max_played_at(self, game_name: str = "") -> datetime:
        """Get the latest played_at timestamp in the matches table for a game (or all games if empty)."""
        gid = self.resolve_game_id(game_name)
        if gid:
            rows = self.client.execute(
                "SELECT max(played_at) FROM matches FINAL WHERE game_id = %(g)s",
                {"g": gid},
            )
        else:
            rows = self.client.execute("SELECT max(played_at) FROM matches FINAL")
        return rows[0][0] if rows and rows[0][0] else None

    def get_last_processed_date(self, game_name: str, rating_system: str):
        """Get the last played_at date processed in rating_history for a game/system."""
        gid = self.resolve_game_id(game_name)
        rows = self.client.execute(
            "SELECT max(played_at) FROM rating_history "
            "WHERE game_id = %(g)s AND rating_system = %(rs)s",
            {"g": gid, "rs": rating_system},
        )
        return rows[0][0] if rows and rows[0][0] else None

    def load_ratings(self, game_name: str, rating_system: str) -> dict:
        """Load current ratings from player_ratings for a game/system.

        Returns dict mapping player_id -> {rating, rd, vol, wins, losses,
        matches, name, last_match_id, last_match_date}.
        """
        rows = self.client.execute(
            "SELECT player_id, rating, rd, vol, wins, losses, "
            "matches_played, last_match_id, last_match_date, first_match_date "
            "FROM player_ratings FINAL "
            "WHERE game_id = %(g)s AND rating_system = %(rs)s",
            {"g": self.resolve_game_id(game_name), "rs": rating_system},
        )
        ratings = {}
        for row in rows:
            pid, rating, rd, vol, wins, losses, matches, last_mid, last_date, first_date = row
            ratings[pid] = {
                "rating": rating,
                "rd": rd,
                "vol": vol,
                "wins": wins,
                "losses": losses,
                "matches": matches,
                "name": "",
                "last_match_id": last_mid,
                "last_match_date": last_date,
                "first_match_date": first_date,
            }
        # Fill names from players table
        if ratings:
            name_rows = self.client.execute(
                "SELECT player_id, name FROM players FINAL WHERE player_id IN %(ids)s",
                {"ids": tuple(ratings.keys())},
            )
            for pid, name in name_rows:
                if pid in ratings:
                    ratings[pid]["name"] = name
        return ratings

    def delete_rating_history_from_date(self, game_name: str, rating_system: str, from_date):
        """Delete rating history rows with played_at >= from_date.

        Used for Glicko-2 incremental: clear the last (possibly incomplete) period
        so it can be recomputed with new matches.
        """
        self.client.execute(
            "ALTER TABLE rating_history DELETE "
            "WHERE game_id = %(g)s AND rating_system = %(rs)s AND played_at >= %(d)s",
            {"g": self.resolve_game_id(game_name), "rs": rating_system, "d": from_date},
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
            "WHERE game_id = %(g)s AND rating_system = %(rs)s AND played_at < %(d)s "
            "GROUP BY player_id",
            {"g": self.resolve_game_id(game_name), "rs": rating_system, "d": before_date},
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
            "WHERE game_id = %(g)s AND rating_system = %(rs)s AND played_at < %(d)s "
            "GROUP BY player_id",
            {"g": self.resolve_game_id(game_name), "rs": rating_system, "d": before_date},
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
        gid = self.resolve_game_id(game_name)
        if gid:
            return self.client.execute(
                "SELECT match_id, player1_id, player2_id, player1_score, player2_score, "
                "winner_id, played_at, game_id, tournament_id "
                "FROM matches FINAL "
                "WHERE game_id = %(g)s AND played_at >= %(d)s "
                "AND " + duel_filter + " "
                "ORDER BY played_at",
                {"g": gid, "d": from_date},
            )
        return self.client.execute(
            "SELECT match_id, player1_id, player2_id, player1_score, player2_score, "
            "winner_id, played_at, game_id, tournament_id "
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
        (in raw_posts) can skip the network fetch. Tournaments with tier but
        no raw_html still need to be fetched.

        Returns dict mapping tournament_id -> tier string.
        """
        rows = self.client.execute(
            "SELECT t.tournament_id, t.tier FROM tournaments t FINAL "
            "JOIN raw_posts rp FINAL ON rp.post_id = t.tournament_id "
            "WHERE rp.raw_html != ''"
        )
        return {tid: tier for tid, tier in rows}
"""ClickHouse DDL schema for Arena Rankings System.

Tables:
  - match_registry: central index of discovered matches + raw HTML storage
  - players: player profiles (ID, name, country)
  - games: game lookup table (game_id -> name)
  - tournaments: tournament metadata (ID, name, tier)
  - matches: parsed match details (player IDs, scores, game_id, tournament_id, date)
  - match_maps: per-map results for each match (map_id only)
  - maps: map lookup table (map_id -> canonical name, image, game)
  - player_aliases: historical name spellings per player (for alias display)
  - player_ratings: computed ratings (Elo, Glicko-2) per player per game

Normalization principle: IDs are the primary keys everywhere. Names live only
in their canonical tables (players, games, tournaments, maps). Hot tables
(matches, match_maps) reference entities by ID only. All IDs come from the
parse step of the pipeline (PlusForward page data), never hand-crafted.
"""

# Drop existing database and recreate
DROP_DATABASE = "DROP DATABASE IF EXISTS arena_rankings"
CREATE_DATABASE = "CREATE DATABASE arena_rankings"

# Individual DROP TABLE statements (for idempotent re-init)
DROP_TABLES = [
    "DROP TABLE IF EXISTS arena_rankings.maps",
    "DROP TABLE IF EXISTS arena_rankings.match_maps",
    "DROP TABLE IF EXISTS arena_rankings.matches",
    "DROP TABLE IF EXISTS arena_rankings.tournaments",
    "DROP TABLE IF EXISTS arena_rankings.games",
    "DROP TABLE IF EXISTS arena_rankings.players",
    "DROP TABLE IF EXISTS arena_rankings.player_aliases",
    "DROP TABLE IF EXISTS arena_rankings.rating_history",
    "DROP TABLE IF EXISTS arena_rankings.player_ratings",
    "DROP TABLE IF EXISTS arena_rankings.match_registry",
    "DROP TABLE IF EXISTS arena_rankings.tournament_brackets",
]

# DDL statements in dependency order
DDL_STATEMENTS = [
    # match_registry — Central index of discovered matches + raw HTML storage
    # match_id is the permanent PlusForward post ID (stable, never shifts)
    # raw_html is empty string until downloaded in Phase 2
    # played_at: when the match was played (from matchlist results page at discovery)
    # status: discovered → downloaded → parsed | failed
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.match_registry (
        match_id UInt64,
        played_at DateTime DEFAULT toDateTime(0),
        raw_html String DEFAULT '',
        status LowCardinality(String) DEFAULT 'discovered'
    )
    ENGINE = ReplacingMergeTree()
    ORDER BY (played_at, match_id)
    """,

    # players — Player profiles extracted from match pages
    # player_id is the PlusForward player ID (from /player/<id>/... URLs)
    # name is the display name; country is the ISO code from the flag icon.
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.players (
        player_id UInt64,
        name String,
        country LowCardinality(String) DEFAULT ''
    )
    ENGINE = ReplacingMergeTree()
    ORDER BY player_id
    """,

    # games — Game lookup table. game_id is the PlusForward category ID
    # (from the pfcat-{id} icon). name is the display name (from the match
    # Description div). Populated by the parser via upsert_game.
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.games (
        game_id UInt32,
        name String DEFAULT ''
    )
    ENGINE = ReplacingMergeTree()
    ORDER BY game_id
    """,

    # tournaments — Tournament metadata extracted from the tournament page itself
    # (PlusForward post page at {BASE_URL}/post/{tournament_id}/). Name, tier, and
    # raw_html are populated by the TournamentResolver when it fetches the page.
    # The *_parsed columns (game, prize, formats, maplist, rankings) are extracted
    # from the same page's .tour_info / .tour_rankings blocks. rankings is a JSON
    # array: [{"position": "1st", "player_id": 123, "player_name": "...", "prize": "60 USD"}]
    # tournament_id is the PlusForward post ID of the tournament page
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.tournaments (
        tournament_id UInt64,
        name String,
        tier LowCardinality(String) DEFAULT '',
        raw_html String DEFAULT '',
        game LowCardinality(String) DEFAULT '',
        prize_money String DEFAULT '',
        tourney_format LowCardinality(String) DEFAULT '',
        match_format LowCardinality(String) DEFAULT '',
        schedule_start DateTime DEFAULT '1970-01-01',
        schedule_end DateTime DEFAULT '1970-01-01',
        maplist Array(String) DEFAULT [],
        rankings String DEFAULT '[]'
    )
    ENGINE = ReplacingMergeTree()
    ORDER BY tournament_id
    """,

    # matches — Parsed match details (fully normalized: IDs only)
    # match_id is the PlusForward post ID (same as match_registry)
    # player1_id/player2_id reference players; winner_id is the winner's player_id
    # game_id references games (was game_category_id, the pfcat category ID)
    # tournament_id references tournaments
    # match_format: e.g. "Best of 5", "Time Limit Duel"
    # played_at: when the match was played (parsed from match page)
    # Denormalized name/country/tournament_name/game_name columns were removed:
    # they duplicated players/games/tournaments. Resolve via JOIN on IDs.
    # Note: status column was removed — it was dead (0 rows ever differed from
    # 'Match finished'). Parsing state lives in match_registry.status instead.
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.matches (
        match_id UInt64,
        player1_id UInt64,
        player2_id UInt64,
        player1_score Int8,
        player2_score Int8,
        winner_id UInt64,
        game_id UInt32 DEFAULT 0,
        match_format LowCardinality(String) DEFAULT '',
        tournament_id UInt64 DEFAULT 0,
        stage_name String DEFAULT '',
        played_at DateTime
    )
    ENGINE = ReplacingMergeTree()
    ORDER BY (game_id, played_at, match_id)
    """,

    # match_maps — Per-map results for each match (fully normalized: IDs only)
    # match_id + map_index uniquely identifies a map within a match
    # map_id is the PlusForward map ID (from the map image URL). 0 = unknown.
    # map_name was removed: it duplicated maps.name. Resolve via maps table.
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.match_maps (
        match_id UInt64,
        map_index UInt8,
        map_id UInt32 DEFAULT 0,
        player1_score Int16,
        player2_score Int16,
        played_at DateTime
    )
    ENGINE = ReplacingMergeTree()
    ORDER BY (played_at, match_id, map_index)
    """,

    # maps — Map lookup table. map_id is the PlusForward map ID (primary key),
    # extracted from the map image URL /files/images/maps/{map_id}_{slug}.jpg.
    # name is the canonical display name. image is the PlusForward image path
    # ('' if unknown). game is the single game this map is played in (from
    # match_maps, with tournament data as fallback for maps never played).
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.maps (
        map_id UInt32,
        name String DEFAULT '',
        image String DEFAULT '',
        game String DEFAULT ''
    )
    ENGINE = ReplacingMergeTree()
    ORDER BY map_id
    """,

    # player_aliases — Historical name spellings per player.
    # Preserves the match-time spelling history that used to live in
    # matches.player1_name/player2_name. Populated by the parser: one row per
    # distinct spelling per player, with a usage count. The canonical (most
    # used) name is the one in players.name; the rest are aliases shown dimmed.
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.player_aliases (
        player_id UInt64,
        name String,
        count UInt32 DEFAULT 0
    )
    ENGINE = ReplacingMergeTree()
    ORDER BY (player_id, name)
    """,

    # discovery_state — Track discovery progress for resume support
    # key: e.g. 'last_known_page', 'forward_scan_complete'
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.discovery_state (
        key String,
        value String
    )
    ENGINE = ReplacingMergeTree()
    ORDER BY key
    """,

    # rating_history — Per-match rating snapshots for building progression graphs
    # One row per player per match per game per rating system
    # Records the rating state *after* that match was processed
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.rating_history (
        player_id UInt64,
        game_id UInt32 DEFAULT 0,
        rating_system LowCardinality(String) DEFAULT 'elo',
        match_id UInt64,
        played_at DateTime,
        rating Float64,
        rd Float64 DEFAULT 0.0,
        vol Float64 DEFAULT 0.0,
        wins UInt32 DEFAULT 0,
        losses UInt32 DEFAULT 0,
        matches_played UInt32 DEFAULT 0
    )
    ENGINE = MergeTree()
    ORDER BY (player_id, game_id, rating_system, played_at, match_id)
    """,

    # player_ratings — Computed ratings per player per game per rating system
    # rating_system: 'elo' or 'glicko2'
    # game_id: PlusForward category ID (0 = combined 'All Games' row).
    # Resolve names via the games table.
    # player_name was removed: it duplicated players.name (source of truth).
    # Resolve names via the players table / matches instead.
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.player_ratings (
        player_id UInt64,
        game_id UInt32 DEFAULT 0,
        rating_system LowCardinality(String) DEFAULT 'elo',
        rating Float64 DEFAULT 1500.0,
        rd Float64 DEFAULT 350.0,  -- Glicko-2 rating deviation
        vol Float64 DEFAULT 0.06,  -- Glicko-2 volatility
        wins UInt32 DEFAULT 0,
        losses UInt32 DEFAULT 0,
        matches_played UInt32 DEFAULT 0,
        last_match_id UInt64 DEFAULT 0,
        last_match_date DateTime DEFAULT toDateTime(0),
        first_match_date DateTime DEFAULT toDateTime(0)
    )
    ENGINE = ReplacingMergeTree()
    ORDER BY (player_id, game_id, rating_system)
    """,

    # tournament_brackets — Cached bracket data for a tournament, fetched from
    # an external bracket provider (Toornament or shambler) whose link is found
    # in the tournament's PlusForward raw_html.
    # source: 'toornament' | 'shambler'
    # data: normalized, source-agnostic JSON describing the bracket structure:
    #   {
    #     "source": "toornament"|"shambler",
    #     "title": "...",
    #     "stages": [{"name": "...", "groups": [{"name": "...",
    #        "rounds": [{"name": "...", "round": 0, "matches": [
    #          {"p1": "name", "p2": "name", "score1": int|null,
    #           "score2": int|null, "winner": "p1"|"p2"|null} ]}]}]}]
    #   }
    # fetched_at: when we last fetched (and stored) this bracket.
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.tournament_brackets (
        tournament_id UInt64,
        source LowCardinality(String) DEFAULT '',
        data String DEFAULT '{}',
        fetched_at DateTime DEFAULT toDateTime(0)
    )
    ENGINE = ReplacingMergeTree()
    ORDER BY tournament_id
    """,
]
"""ClickHouse DDL schema for Arena Rankings System.

Tables:
  - match_registry: central index of discovered matches + raw HTML storage
  - players: player profiles (ID, name, country)
  - tournaments: tournament metadata (ID, name, tier)
  - matches: parsed match details (players, scores, tournament, date, maps)
  - match_maps: per-map results for each match
  - maps: map lookup table (map_id -> canonical name, slug, image, game)
  - player_ratings: computed ratings (Elo, Glicko-2) per player per game
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
    "DROP TABLE IF EXISTS arena_rankings.players",
    "DROP TABLE IF EXISTS arena_rankings.rating_history",
    "DROP TABLE IF EXISTS arena_rankings.player_ratings",
    "DROP TABLE IF EXISTS arena_rankings.match_registry",
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
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.players (
        player_id UInt64,
        name String,
        country LowCardinality(String) DEFAULT ''
    )
    ENGINE = ReplacingMergeTree()
    ORDER BY player_id
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

    # matches — Parsed match details
    # match_id is the PlusForward post ID (same as match_registry)
    # winner_id is the player_id of the winner
    # game_name: e.g. "Quake Champions", "Quake Live"
    # match_format: e.g. "Best of 5", "Time Limit Duel"
    # played_at: when the match was played (parsed from match page)
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.matches (
        match_id UInt64,
        player1_id UInt64,
        player2_id UInt64,
        player1_name String,
        player2_name String,
        player1_country LowCardinality(String) DEFAULT '',
        player2_country LowCardinality(String) DEFAULT '',
        player1_score Int8,
        player2_score Int8,
        winner_id UInt64,
        game_name LowCardinality(String) DEFAULT '',
        game_category_id UInt32 DEFAULT 0,
        match_format LowCardinality(String) DEFAULT '',
        tournament_id UInt64 DEFAULT 0,
        tournament_name String DEFAULT '',
        stage_name String DEFAULT '',
        played_at DateTime,
        status LowCardinality(String) DEFAULT ''
    )
    ENGINE = ReplacingMergeTree()
    ORDER BY (game_name, played_at, match_id)
    """,

    # match_maps — Per-map results for each match
    # match_id + map_index uniquely identifies a map within a match
    # map_id is the PlusForward map ID (from the map image URL). 0 = unknown.
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.match_maps (
        match_id UInt64,
        map_index UInt8,
        map_id UInt32 DEFAULT 0,
        map_name LowCardinality(String),
        player1_name String,
        player2_name String,
        player1_score Int16,
        player2_score Int16,
        played_at DateTime
    )
    ENGINE = ReplacingMergeTree()
    ORDER BY (played_at, match_id, map_index)
    """,

    # maps — Map lookup table. map_id is the PlusForward map ID (primary key),
    # extracted from the map image URL /files/images/maps/{map_id}_{slug}.jpg.
    # name is the canonical display name; slug is the URL slug (cosmetic, like
    # players). image is the PlusForward image path ('' if unknown). game is the
    # single game this map is played in (from match_maps, with tournament data
    # as fallback for maps never played).
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.maps (
        map_id UInt32,
        name String DEFAULT '',
        slug String DEFAULT '',
        image String DEFAULT '',
        game String DEFAULT ''
    )
    ENGINE = ReplacingMergeTree()
    ORDER BY map_id
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
        game_name LowCardinality(String) DEFAULT '',
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
    ORDER BY (player_id, game_name, rating_system, played_at, match_id)
    """,

    # player_ratings — Computed ratings per player per game per rating system
    # rating_system: 'elo' or 'glicko2'
    # game_name: 'Quake Champions', 'Quake Live', etc. (empty = combined)
    # game_id: 0 = combined, >0 = specific game
    """
    CREATE TABLE IF NOT EXISTS arena_rankings.player_ratings (
        player_id UInt64,
        player_name String,
        game_name LowCardinality(String) DEFAULT '',
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
    ORDER BY (player_id, game_name, rating_system)
    """,
]
"""Rankings computation — Elo and Glicko-2 rating systems.

Processes parsed matches from ClickHouse and computes player ratings.

Usage:
    python -m src.rankings_compute                  # compute all ratings
    python -m src.rankings_compute --game "Quake Champions"  # specific game
    python -m src.rankings_compute --system elo    # specific rating system
    python -m src.rankings_compute -v               # verbose logging
"""

import logging
import math
from collections import defaultdict
from datetime import datetime

from config import ELO_K_BASE, ELO_TIER_MULTIPLIER, DEFAULT_TIER_MULTIPLIER, GLICKO2_TAU, GLICKO2_INITIAL_VOL, GLICKO2_PERIOD
from src.db_client import Database

logger = logging.getLogger(__name__)


def _base_k_factor(games_played: int) -> float:
    """Experience-based base K-factor.

    - <30 games: K=40 (provisional, fast convergence)
    - 30-100 games: K=24 (established)
    - >100 games: K=16 (veteran, stable ratings)
    """
    if games_played < 30:
        return float(ELO_K_BASE["provisional"])
    elif games_played <= 100:
        return float(ELO_K_BASE["established"])
    else:
        return float(ELO_K_BASE["veteran"])


def _match_result(p1_id: int, p2_id: int, p1_score: int, p2_score: int,
                  winner_id: int) -> tuple[float, float, str]:
    """Determine the result (s1, s2, outcome) of a 1v1 match.

    Honors the explicitly recorded winner (winner_id) even when the score is
    equal (0:0 — e.g. forfeit/walkover where plusforward still marks a winner).
    Falls back to score comparison, then to a draw (0.5/0.5) when scores are
    equal and no winner is marked.

    Returns (s1, s2, outcome) where outcome is 'p1' | 'p2' | 'draw' and s1/s2
    are the rating outcomes (1.0/0.0/0.5) for players 1 and 2.
    """
    if winner_id and winner_id == p1_id:
        return 1.0, 0.0, 'p1'
    if winner_id and winner_id == p2_id:
        return 0.0, 1.0, 'p2'
    if p1_score > p2_score:
        return 1.0, 0.0, 'p1'
    if p2_score > p1_score:
        return 0.0, 1.0, 'p2'
    return 0.5, 0.5, 'draw'


def _k_factor_for_tier(tier: str, games_played: int) -> float:
    """Get Elo K-factor: base (experience) × tier multiplier.

    K = base_K(games_played) × tier_multiplier

    Tier multipliers:
        premier: ×2.0, major: ×1.5, minor: ×1.0
    Unknown tier defaults to ×1.0 (same as minor).
    """
    base = _base_k_factor(games_played)
    if tier and tier.lower() in ELO_TIER_MULTIPLIER:
        multiplier = float(ELO_TIER_MULTIPLIER[tier.lower()])
    else:
        multiplier = DEFAULT_TIER_MULTIPLIER
    return base * multiplier


def _game_label(game_name: str) -> str:
    """Format game name for log messages — empty string becomes 'All Games'."""
    return game_name if game_name else "All Games"


def _system_display(system: str) -> str:
    """Format rating system name for log messages — internal lowercase → display name."""
    return "Glicko-2" if system == "glicko2" else "Elo"


def _period_key(played_at: datetime, period: str) -> tuple:
    """Extract rating period key from a datetime.

    Args:
        played_at: Match timestamp.
        period: 'year', 'month', 'week', or 'day'.

    Returns:
        Tuple usable as dict key and for sorting.
    """
    if period == "year":
        return (played_at.year,)
    elif period == "month":
        return (played_at.year, played_at.month)
    elif period == "week":
        iso = played_at.isocalendar()
        return (iso[0], iso[1])  # ISO year, week number
    else:  # day
        return (played_at.year, played_at.month, played_at.day)


def _period_start(played_at: datetime, period: str) -> datetime:
    """Get the start (midnight) of the rating period containing played_at.

    Used for incremental reload: reload all matches from the start of the
    last processed period to catch matches added to the same period.
    """
    if period == "year":
        return datetime(played_at.year, 1, 1)
    elif period == "month":
        return datetime(played_at.year, played_at.month, 1)
    elif period == "week":
        # Monday of the ISO week, at midnight
        from datetime import timedelta
        monday = played_at - timedelta(days=played_at.weekday())
        return datetime(monday.year, monday.month, monday.day)
    else:  # day
        return datetime(played_at.year, played_at.month, played_at.day)


def _period_ch_function(period: str) -> str:
    """ClickHouse truncation function for the safety check (distinct period count).

    Returns SQL expression that extracts a comparable period value from played_at.
    """
    if period == "year":
        return "toYear(played_at)"
    elif period == "month":
        return "toYYYYMM(played_at)"
    else:  # day
        return "toDate(played_at)"


def _period_label(period: str) -> str:
    """Human-readable period name for log messages."""
    return {"year": "year", "month": "month", "week": "week", "day": "day"}.get(period, period)


# --- Elo ---

class EloRating:
    """Standard Elo rating system with variable K-factor."""

    def __init__(self, k: float = 32.0, initial: float = 1500.0):
        self.k = k
        self.initial = initial

    def k_factor(self, games_played: int) -> float:
        """Variable K-factor based on games played (experience only, no tier).

        Uses config ELO_K_BASE values:
        - <30 games: K=40 (provisional, fast convergence)
        - 30-100 games: K=24 (established)
        - >100 games: K=16 (veteran, stable ratings)
        """
        return _base_k_factor(games_played)

    def expected_score(self, rating_a: float, rating_b: float) -> float:
        """Expected score for player A against player B (0 to 1)."""
        return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))

    def update(self, rating_a: float, rating_b: float, score_a: float,
               k_a: float = None, k_b: float = None) -> tuple[float, float]:
        """Update ratings after a match.

        Args:
            rating_a: Current rating of player A.
            rating_b: Current rating of player B.
            score_a: Actual score for player A (1 = win, 0 = loss, 0.5 = draw).
            k_a: K-factor for player A (defaults to self.k).
            k_b: K-factor for player B (defaults to self.k).

        Returns:
            (new_rating_a, new_rating_b)
        """
        if k_a is None:
            k_a = self.k
        if k_b is None:
            k_b = self.k

        expected_a = self.expected_score(rating_a, rating_b)
        expected_b = 1.0 - expected_a
        score_b = 1.0 - score_a

        new_a = rating_a + k_a * (score_a - expected_a)
        new_b = rating_b + k_b * (score_b - expected_b)
        return new_a, new_b


# --- Glicko-2 ---

class Glicko2Rating:
    """Glicko-2 rating system.

    Based on: http://www.glicko.net/glicko/glicko2.pdf
    """

    # Glicko-2 constants
    SCALE = 173.7178  # 400 / ln(10)
    INITIAL_RATING = 1500.0
    INITIAL_RD = 350.0
    INITIAL_VOL = GLICKO2_INITIAL_VOL
    TAU = GLICKO2_TAU  # system constant (constrains volatility change)

    def __init__(self):
        pass

    def _g(self, rd: float) -> float:
        """Glicko-2 g(RD) function."""
        return 1.0 / math.sqrt(1.0 + 3.0 * (rd ** 2) / (self.SCALE ** 2))

    def _expected(self, rating: float, opp_rating: float, opp_rd: float) -> float:
        """Expected score against an opponent."""
        g = self._g(opp_rd)
        return 1.0 / (1.0 + math.exp(-g * (rating - opp_rating) / self.SCALE))

    def _update_volatility(self, rd: float, vol: float, v: float, delta: float) -> float:
        """Update player volatility using the Glicko-2 algorithm."""
        a = math.log(vol ** 2)

        # Precompute
        rd_sq = rd ** 2
        delta_sq = delta ** 2

        # Step 4: Compute f(x)
        def f(x):
            num = math.exp(x) * (delta_sq - rd_sq - v - math.exp(x))
            den = 2.0 * (rd_sq + v + math.exp(x)) ** 2
            return num / den - (x - a) / (self.TAU ** 2)

        # Step 5: Iterative algorithm (Illinois method)
        epsilon = 1e-6
        A = a
        B = None

        if delta_sq > rd_sq + v:
            B = math.log(delta_sq - rd_sq - v)
        else:
            k = 1
            while f(a - k * self.TAU ** 2) < 0:
                k += 1
            B = a - k * self.TAU ** 2

        fA = f(A)
        fB = f(B)

        while abs(B - A) > epsilon:
            C = A + (A - B) * fA / (fB - fA)
            fC = f(C)
            if fC * fB <= 0:
                A = B
                fA = fB
            else:
                fA = fA / 2.0
            B = C
            fB = fC

        return math.exp(A / 2.0)

    def update_player(
        self,
        rating: float,
        rd: float,
        vol: float,
        opponents: list[tuple[float, float, float]],
    ) -> tuple[float, float, float]:
        """Update a player's rating after a rating period.

        Args:
            rating: Current rating.
            rd: Current rating deviation.
            vol: Current volatility.
            opponents: List of (opp_rating, opp_rd, score) tuples.

        Returns:
            (new_rating, new_rd, new_vol)
        """
        if not opponents:
            # No games in this period — RD increases (scaled: phi' = sqrt(phi^2 + sigma^2))
            new_rd = self.SCALE * math.sqrt((rd / self.SCALE) ** 2 + vol ** 2)
            new_rd = min(new_rd, self.INITIAL_RD)
            return rating, new_rd, vol

        # Step 1: Convert to Glicko-2 scale
        mu = (rating - self.INITIAL_RATING) / self.SCALE
        phi = rd / self.SCALE

        # Step 2: Compute v (estimated variance)
        g_values = [self._g(opp_rd) for _, opp_rd, _ in opponents]
        expected = [self._expected(rating, opp_r, opp_rd) for opp_r, opp_rd, _ in opponents]
        v = 1.0 / sum(g ** 2 * e * (1 - e) for g, e in zip(g_values, expected))

        # Step 3: Compute delta
        delta_sum = sum(g * (s - e) for g, s, e in zip(g_values, [s for _, _, s in opponents], expected))
        delta = v * delta_sum

        # Step 4-5: Update volatility (uses scaled phi, not unscaled rd)
        new_vol = self._update_volatility(phi, vol, v, delta)

        # Step 6: Update phi
        new_phi = math.sqrt(1.0 / (1.0 / phi ** 2 + 1.0 / v) + new_vol ** 2)
        if math.isnan(new_phi) or new_phi == 0:
            new_phi = phi  # fallback

        # Step 7: Update mu
        new_mu = mu + new_phi ** 2 * delta_sum

        # Convert back
        new_rating = self.SCALE * new_mu + self.INITIAL_RATING
        new_rd = self.SCALE * new_phi

        # Clamp RD
        new_rd = min(new_rd, self.INITIAL_RD)

        return new_rating, new_rd, new_vol


# --- Rankings Computation ---

def _fill_player_names(db: Database, ratings: dict):
    """Fill player names from the players table (source of truth)."""
    if not ratings:
        return

    player_ids = list(ratings.keys())
    name_rows = db.client.execute(
        "SELECT player_id, name FROM players FINAL WHERE player_id IN %(ids)s",
        {"ids": tuple(player_ids)},
    )
    for pid, name in name_rows:
        if pid in ratings:
            ratings[pid]["name"] = name

    # Fallback placeholder for players with no name row
    for pid, d in ratings.items():
        if not d.get("name"):
            d["name"] = f"player_{pid}"


def _check_match_state(db: Database, game_name: str, rating_system: str) -> tuple[str, int, int]:
    """Determine whether ratings need updating and how.

    Returns (state, total_in_db, total_in_hist) where state is one of:
      - 'up_to_date': no new matches, no backfill — skip entirely
      - 'new_matches': matches added at the end — incremental update
      - 'backfill': older matches added (or matches removed) — full recompute

    Detection logic:
      - Compare min/max match_id in matches table vs Elo's rating_history
        (Elo stores one row per player per match, so its match_ids accurately
        reflect all processed matches. Glicko-2 stores one row per player per
        period, so its match_ids can't be used for match-level comparison.)
      - max(hist) < max(db) → new matches at the end → incremental
      - min(hist) > min(db) → older matches appeared → backfill
      - Both same → up to date

    Both Elo and Glicko-2 use this function so they stay in sync: they both
    compare against Elo's history, which is the authoritative match tracker.

    Important: must be called BEFORE Elo stores its updated history, otherwise
    Glicko-2 will see the already-updated history and skip. The rank.py loop
    pre-computes states for all games before any computation/store happens.
    """
    duel_filter = "((match_format ILIKE '%%duel%%' AND match_format NOT ILIKE '%%team%%') OR match_format ILIKE '%%1v1%%')"
    # Always compare against Elo's history — it tracks every match individually.
    hist_system = "elo"

    gid = db.resolve_game_id(game_name)
    if gid:
        db_minmax = db.client.execute(
            f"SELECT min(match_id), max(match_id), count() FROM matches FINAL WHERE game_id = %(g)s AND {duel_filter}",
            {"g": gid}
        )[0]
        hist_minmax = db.client.execute(
            "SELECT min(match_id), max(match_id), count(DISTINCT match_id) FROM rating_history WHERE rating_system = %(rs)s AND game_id = %(g)s",
            {"rs": hist_system, "g": gid}
        )[0]
    else:
        db_minmax = db.client.execute(
            f"SELECT min(match_id), max(match_id), count() FROM matches FINAL WHERE {duel_filter}"
        )[0]
        hist_minmax = db.client.execute(
            "SELECT min(match_id), max(match_id), count(DISTINCT match_id) FROM rating_history WHERE rating_system = %(rs)s",
            {"rs": hist_system}
        )[0]

    db_min, db_max, db_count = db_minmax
    hist_min, hist_max, hist_count = hist_minmax

    # Handle empty states
    if db_count == 0:
        return "up_to_date", db_count, hist_count
    if hist_count == 0:
        return "backfill", db_count, hist_count  # no history → need full recompute

    if hist_min > db_min:
        # Older matches appeared in the DB → backfill
        return "backfill", db_count, hist_count
    if hist_max < db_max:
        # New matches at the end → incremental
        return "new_matches", db_count, hist_count
    # min and max both match — but count could still differ (matches removed/replaced)
    if db_count != hist_count:
        return "backfill", db_count, hist_count

    # Self-healing: if the stored ratings are corrupted (NaN/Inf/out-of-range),
    # force a full recompute even though the match_ids look consistent. This
    # catches "running but broken" states (e.g. a bad write, schema change, or
    # partial import) that the match_id comparison alone would miss.
    bad = db.client.execute(
        "SELECT count() FROM player_ratings FINAL "
        "WHERE isNaN(rating) OR isInfinite(rating) OR isNaN(rd) OR isInfinite(rd) "
        "OR rating < 0 OR rating > 10000 OR rd < 0 OR rd > 10000"
    )[0][0]
    if bad:
        logger.warning(f"{_game_label(game_name)}: {bad} corrupted rating rows, forcing full recompute")
        return "backfill", db_count, hist_count

    return "up_to_date", db_count, hist_count


def compute_elo(db: Database, game_name: str = "", full_recompute: bool = False, match_state: str | None = None, match_counts: tuple[int, int] | None = None) -> dict[int, dict]:
    """Compute Elo ratings for all players.

    Uses variable K-factor: K=40 for new players (<30 games),
    K=32 for experienced players (30-100 games), K=24 for veterans (100+ games).

    Args:
        db: Database client.
        game_name: Filter by game (empty = all games).
        full_recompute: If True, compute from scratch. If False, load existing
            ratings and only process new matches since last computation.
        match_state: Pre-computed state from _check_match_state. If provided,
            skips the check and uses this state directly.

    Returns:
        Dict mapping player_id -> {rating, wins, losses, matches, name, last_match_id, last_match_date, history}
        where history is a list of (player_id, game_name, system, match_id, played_at, rating, rd, vol, wins, losses, matches_played) tuples.
    """
    if full_recompute:
        db.clear_ratings(rating_system="elo", game_name=game_name)
        db.clear_rating_history(rating_system="elo", game_name=game_name)
        matches = db.get_all_matches_for_game(game_name)
        ratings = defaultdict(lambda: {"rating": 1500.0, "wins": 0, "losses": 0, "matches": 0, "name": "", "last_match_id": 0, "last_match_date": datetime(1970, 1, 1), "first_match_date": datetime(1970, 1, 1)})
        logger.info(f"Elo {_game_label(game_name)}: from scratch, {len(matches)} matches")
    else:
        # Incremental: load existing ratings, find where we left off
        existing = db.load_ratings(game_name, "elo")
        if not existing:
            # No previous computation — start fresh
            matches = db.get_all_matches_for_game(game_name)
            ratings = defaultdict(lambda: {"rating": 1500.0, "wins": 0, "losses": 0, "matches": 0, "name": "", "last_match_id": 0, "last_match_date": datetime(1970, 1, 1), "first_match_date": datetime(1970, 1, 1)})
            logger.info(f"Elo {_game_label(game_name)}: from scratch, {len(matches)} matches [no prior]")
        else:
            ratings = defaultdict(lambda: {"rating": 1500.0, "wins": 0, "losses": 0, "matches": 0, "name": "", "last_match_id": 0, "last_match_date": datetime(1970, 1, 1), "first_match_date": datetime(1970, 1, 1)}, existing)

            # Check match state: up_to_date / new_matches / backfill
            if match_state is not None:
                state = match_state
                total_in_db, total_in_hist = match_counts if match_counts else (0, 0)
            else:
                state, total_in_db, total_in_hist = _check_match_state(db, game_name, "elo")
            if state == "up_to_date":
                return None
            elif state == "backfill":
                matches = db.get_all_matches_for_game(game_name)
                ratings = defaultdict(lambda: {"rating": 1500.0, "wins": 0, "losses": 0, "matches": 0, "name": "", "last_match_id": 0, "last_match_date": datetime(1970, 1, 1), "first_match_date": datetime(1970, 1, 1)})
                db.clear_ratings(rating_system="elo", game_name=game_name)
                db.clear_rating_history(rating_system="elo", game_name=game_name)
                logger.info(f"Elo {_game_label(game_name)}: full recompute, {len(matches)} matches [backfill]")
            else:  # new_matches
                last_match_id = db.get_last_processed_match_id(game_name, "elo")
                last_time = db.get_last_processed_match_time(game_name, "elo")
                matches = db.get_matches_for_game_after(game_name, last_match_id)
                if matches:
                    # Detect out-of-order arrivals: a newly parsed match played
                    # EARLIER than the newest already-rated match means match_ids
                    # aren't monotonic with played_at (live tournaments can parse
                    # matches out of chronological order). Incremental chaining
                    # would rate them with stale ratings, corrupting history — so
                    # do a full recompute in correct order. (A surgical tail
                    # delete via ALTER TABLE DELETE is not used: that mutation
                    # isn't reliably visible to FINAL reads in this ClickHouse
                    # setup, and the full recompute is only ~0.2s anyway.)
                    earliest_new = min((r[6] for r in matches), default=None)
                    if last_time is not None and earliest_new is not None and earliest_new < last_time:
                        logger.warning(
                            f"Elo {_game_label(game_name)}: out-of-order matches "
                            f"(earliest new {earliest_new} < last rated {last_time}), full recompute")
                        matches = db.get_all_matches_for_game(game_name)
                        ratings = defaultdict(lambda: {"rating": 1500.0, "wins": 0, "losses": 0, "matches": 0, "name": "", "last_match_id": 0, "last_match_date": datetime(1970, 1, 1), "first_match_date": datetime(1970, 1, 1)})
                        db.clear_ratings(rating_system="elo", game_name=game_name)
                        db.clear_rating_history(rating_system="elo", game_name=game_name)
                        logger.info(f"Elo {_game_label(game_name)}: full recompute, {len(matches)} matches [out-of-order]")
                    else:
                        logger.info(f"Elo {_game_label(game_name)}: incremental, {len(matches)} new")
                else:
                    return None

    if not matches:
        return None

    elo = EloRating()
    history = []
    gid = db.resolve_game_id(game_name)

    # Load tournament tiers for tier-based K-factor
    tournament_tiers = db.get_tournament_tiers()

    for row in matches:
        match_id, p1_id, p2_id, p1_score, p2_score, winner_id, played_at, game, tournament_id = row

        r1 = ratings[p1_id]["rating"]
        r2 = ratings[p2_id]["rating"]

        # K-factor: tier-based if available, otherwise experience-based
        tier = tournament_tiers.get(tournament_id, "")
        k1 = _k_factor_for_tier(tier, ratings[p1_id]["matches"])
        k2 = _k_factor_for_tier(tier, ratings[p2_id]["matches"])

        # Determine actual score (1 = win, 0 = loss, 0.5 = draw).
        # Honor the recorded winner_id (e.g. 0:0 forfeit wins) — a marked
        # winner counts as a win even at equal scores.
        s1, s2, outcome = _match_result(p1_id, p2_id, p1_score, p2_score, winner_id)
        if outcome == 'p1':
            ratings[p1_id]["wins"] += 1
            ratings[p2_id]["losses"] += 1
        elif outcome == 'p2':
            ratings[p1_id]["losses"] += 1
            ratings[p2_id]["wins"] += 1

        new_r1, new_r2 = elo.update(r1, r2, s1, k_a=k1, k_b=k2)
        ratings[p1_id]["rating"] = new_r1
        ratings[p2_id]["rating"] = new_r2
        ratings[p1_id]["matches"] += 1
        ratings[p2_id]["matches"] += 1
        ratings[p1_id]["last_match_id"] = match_id
        ratings[p2_id]["last_match_id"] = match_id
        ratings[p1_id]["last_match_date"] = played_at
        ratings[p2_id]["last_match_date"] = played_at
        if ratings[p1_id]["first_match_date"] == datetime(1970, 1, 1):
            ratings[p1_id]["first_match_date"] = played_at
        if ratings[p2_id]["first_match_date"] == datetime(1970, 1, 1):
            ratings[p2_id]["first_match_date"] = played_at

        # Record history snapshot for both players
        history.append((p1_id, gid, "elo", match_id, played_at, new_r1, 0.0, 0.0, ratings[p1_id]["wins"], ratings[p1_id]["losses"], ratings[p1_id]["matches"]))
        history.append((p2_id, gid, "elo", match_id, played_at, new_r2, 0.0, 0.0, ratings[p2_id]["wins"], ratings[p2_id]["losses"], ratings[p2_id]["matches"]))

    _fill_player_names(db, ratings)
    result = dict(ratings)
    result["_history"] = history
    return result


def compute_glicko2(db: Database, game_name: str = "", full_recompute: bool = False, period: str = "year", match_state: str | None = None, match_counts: tuple[int, int] | None = None) -> dict[int, dict]:
    """Compute Glicko-2 ratings for all players.

    Groups matches into rating periods and processes each period.
    Inactive player RD increase is tracked only between rating periods (periods with matches),
    not every calendar period — this keeps it O(active_players * periods).

    Args:
        db: Database client.
        game_name: Filter by game (empty = all games).
        full_recompute: If True, compute from scratch. If False, load existing
            ratings, reload the last rating period (it may be incomplete), and
            process new periods since then.
        period: Rating period granularity — 'year', 'month', 'week', or 'day'.
            Default 'year' (Glicko recommends 10-15 games per player per period;
            yearly gives the most matches per period for our data volume).
        match_state: Pre-computed state from _check_match_state. If provided,
            skips the check and uses this state directly.

    Returns:
        Dict mapping player_id -> {rating, rd, vol, wins, losses, matches, name, last_match_id, last_match_date, history}
    """
    glicko = Glicko2Rating()
    existing = None
    last_date = None

    if full_recompute:
        db.clear_ratings(rating_system="glicko2", game_name=game_name)
        db.clear_rating_history(rating_system="glicko2", game_name=game_name)
        matches = db.get_all_matches_for_game(game_name)
        ratings = defaultdict(lambda: {
            "rating": glicko.INITIAL_RATING,
            "rd": glicko.INITIAL_RD,
            "vol": glicko.INITIAL_VOL,
            "wins": 0,
            "losses": 0,
            "matches": 0,
            "name": "",
            "last_match_id": 0,
            "last_match_date": datetime(1970, 1, 1),
            "first_match_date": datetime(1970, 1, 1),
        })
        # Reset wins/losses/matches to 0 for full recompute — they'll be recounted
        logger.info(f"Glicko-2 {_game_label(game_name)}: from scratch, {len(matches)} matches")
    else:
        # Incremental: load existing ratings
        existing = db.load_ratings(game_name, "glicko2")
        if not existing:
            # No previous computation — start fresh
            matches = db.get_all_matches_for_game(game_name)
            ratings = defaultdict(lambda: {
                "rating": glicko.INITIAL_RATING,
                "rd": glicko.INITIAL_RD,
                "vol": glicko.INITIAL_VOL,
                "wins": 0,
                "losses": 0,
                "matches": 0,
                "name": "",
                "last_match_id": 0,
                "last_match_date": datetime(1970, 1, 1),
                "first_match_date": datetime(1970, 1, 1),
            })
            logger.info(f"Glicko-2 {_game_label(game_name)}: from scratch, {len(matches)} matches [no prior]")
        else:
            ratings = defaultdict(lambda: {
                "rating": glicko.INITIAL_RATING,
                "rd": glicko.INITIAL_RD,
                "vol": glicko.INITIAL_VOL,
                "wins": 0,
                "losses": 0,
                "matches": 0,
                "name": "",
                "last_match_id": 0,
                "last_match_date": datetime(1970, 1, 1),
                "first_match_date": datetime(1970, 1, 1),
            }, existing)

            # Check match state: up_to_date / new_matches / backfill
            # Uses the same _check_match_state function as Elo so both systems
            # trigger the same action (skip / incremental / full recompute) in sync.
            if match_state is not None:
                state = match_state
                total_in_db, total_in_hist = match_counts if match_counts else (0, 0)
            else:
                state, total_in_db, total_in_hist = _check_match_state(db, game_name, "glicko2")
            if state == "up_to_date":
                return None
            elif state == "backfill":
                matches = db.get_all_matches_for_game(game_name)
                ratings = defaultdict(lambda: {
                    "rating": glicko.INITIAL_RATING,
                    "rd": glicko.INITIAL_RD,
                    "vol": glicko.INITIAL_VOL,
                    "wins": 0,
                    "losses": 0,
                    "matches": 0,
                    "name": "",
                    "last_match_id": 0,
                    "last_match_date": datetime(1970, 1, 1),
                    "first_match_date": datetime(1970, 1, 1),
                    })
                db.clear_ratings(rating_system="glicko2", game_name=game_name)
                db.clear_rating_history(rating_system="glicko2", game_name=game_name)
                logger.info(f"Glicko-2 {_game_label(game_name)}: full recompute, {len(matches)} matches [backfill]")
            else:  # new_matches — incremental update
                # Find the last processed date and reload from there
                last_date = db.get_last_processed_date(game_name, "glicko2")
                if last_date is None:
                    matches = db.get_all_matches_for_game(game_name)
                    logger.info(f"Glicko-2 {_game_label(game_name)}: from scratch, {len(matches)} matches [no history]")
                else:
                    # Reload matches from the last processed period forward
                    # The last period may have been incomplete (more matches added to same period)
                    # Reload from the start of that period to catch all matches in the same rating period
                    period_start = _period_start(last_date, period)
                    matches = db.get_matches_for_game_from_date(game_name, period_start)

                    # Load player state (rating/rd/vol + stats) as it was BEFORE the last period.
                    # This gives us the correct pre-period state, avoiding the problem of using
                    # post-update values from player_ratings (which have already been updated).
                    state_before = db.get_state_before_date(game_name, "glicko2", period_start)

                    # Identify players who appear in the reloaded matches
                    players_in_period = set()
                    for m in matches:
                        players_in_period.add(m[1])  # p1_id
                        players_in_period.add(m[2])  # p2_id

                    # For players in the period but NOT in state_before (no prior history),
                    # reset to initial values. Without this, they keep the already-computed
                    # stats from player_ratings and the period's matches get double-counted.
                    glicko_init = {
                        "rating": glicko.INITIAL_RATING,
                        "rd": glicko.INITIAL_RD,
                        "vol": glicko.INITIAL_VOL,
                        "wins": 0,
                        "losses": 0,
                        "matches": 0,
                    }
                    for pid in players_in_period:
                        if pid not in state_before:
                            ratings[pid].update(glicko_init)
                            ratings[pid]["first_match_date"] = datetime(1970, 1, 1)

                    for pid, s in state_before.items():
                        if pid in ratings:
                            ratings[pid]["rating"] = s["rating"]
                            ratings[pid]["rd"] = s["rd"]
                            ratings[pid]["vol"] = s["vol"]
                            ratings[pid]["wins"] = s["wins"]
                            ratings[pid]["losses"] = s["losses"]
                            ratings[pid]["matches"] = s["matches"]

                    # Delete history for the last period so we don't get duplicates
                    db.delete_rating_history_from_date(game_name, "glicko2", period_start)

                    logger.info(f"Glicko-2 {_game_label(game_name)}: incremental, {len(matches)} from {period_start}")

    if not matches:
        return None

    history = []
    gid = db.resolve_game_id(game_name)

    # Group matches into rating periods
    periods = defaultdict(list)
    for row in matches:
        match_id, p1_id, p2_id, p1_score, p2_score, winner_id, played_at, game, tournament_id = row
        key = _period_key(played_at, period)
        periods[key].append(row)

    sorted_periods = sorted(periods.keys())

    # Process each rating period
    for key in sorted_periods:
        period_matches = periods[key]

        # Before processing this period: increase RD for players who played before
        # but are not active in this period (they've been inactive since their last match)
        active_this_period = set()
        for row in period_matches:
            active_this_period.add(row[1])  # p1_id
            active_this_period.add(row[2])  # p2_id

        for pid in ratings:
            if pid not in active_this_period:
                rd = ratings[pid]["rd"]
                vol = ratings[pid]["vol"]
                # RD increases for inactive players (scaled: phi' = sqrt(phi^2 + sigma^2))
                new_rd = glicko.SCALE * math.sqrt((rd / glicko.SCALE) ** 2 + vol ** 2)
                ratings[pid]["rd"] = min(new_rd, glicko.INITIAL_RD)

        # Collect opponents for each player in this period
        player_opponents = defaultdict(list)
        # Track the last match_id and played_at each player had in this period
        period_last_match = {}
        for row in period_matches:
            match_id, p1_id, p2_id, p1_score, p2_score, winner_id, played_at, game, tournament_id = row

            r1 = ratings[p1_id]["rating"]
            rd1 = ratings[p1_id]["rd"]
            r2 = ratings[p2_id]["rating"]
            rd2 = ratings[p2_id]["rd"]

            if p1_score > p2_score:
                s1, s2 = 1.0, 0.0
                ratings[p1_id]["wins"] += 1
                ratings[p2_id]["losses"] += 1
            elif p2_score > p1_score:
                s1, s2 = 0.0, 1.0
                ratings[p1_id]["losses"] += 1
                ratings[p2_id]["wins"] += 1
            else:
                s1, s2 = 0.5, 0.5

            player_opponents[p1_id].append((r2, rd2, s1))
            player_opponents[p2_id].append((r1, rd1, s2))

            ratings[p1_id]["matches"] += 1
            ratings[p2_id]["matches"] += 1
            ratings[p1_id]["last_match_id"] = match_id
            ratings[p2_id]["last_match_id"] = match_id
            ratings[p1_id]["last_match_date"] = played_at
            ratings[p2_id]["last_match_date"] = played_at
            if ratings[p1_id]["first_match_date"] == datetime(1970, 1, 1):
                ratings[p1_id]["first_match_date"] = played_at
            if ratings[p2_id]["first_match_date"] == datetime(1970, 1, 1):
                ratings[p2_id]["first_match_date"] = played_at
            period_last_match[p1_id] = (match_id, played_at)
            period_last_match[p2_id] = (match_id, played_at)

        # Update each player who played in this period
        for pid, opponents in player_opponents.items():
            r = ratings[pid]["rating"]
            rd = ratings[pid]["rd"]
            vol = ratings[pid]["vol"]
            new_r, new_rd, new_vol = glicko.update_player(r, rd, vol, opponents)
            ratings[pid]["rating"] = new_r
            ratings[pid]["rd"] = new_rd
            ratings[pid]["vol"] = new_vol

            # Record history snapshot at end of this rating period
            last_mid, last_played = period_last_match[pid]
            history.append((pid, gid, "glicko2", last_mid, last_played, new_r, new_rd, new_vol, ratings[pid]["wins"], ratings[pid]["losses"], ratings[pid]["matches"]))

    _fill_player_names(db, ratings)
    result = dict(ratings)
    result["_history"] = history
    return result


def store_ratings(db: Database, ratings: dict, game_name: str, system: str):
    """Store computed ratings in ClickHouse (batch insert).

    Also stores rating history snapshots if present.
    """
    history = ratings.pop("_history", [])
    gid = db.resolve_game_id(game_name)
    rows = []
    for pid, data in ratings.items():
        rows.append((
            pid,
            gid,
            system,
            data["rating"],
            data.get("rd") if system == "glicko2" else 0.0,
            data.get("vol") if system == "glicko2" else 0.0,
            data["wins"],
            data["losses"],
            data["matches"],
            data["last_match_id"],
            data["last_match_date"],
            data["first_match_date"],
        ))
    db.upsert_ratings_batch(rows)
    logger.info(
        f"{_system_display(system)} {_game_label(game_name)}: {len(ratings)} players" + (f", {len(history)} hist" if history else "")
    )

    if history:
        db.insert_rating_history_batch(history)

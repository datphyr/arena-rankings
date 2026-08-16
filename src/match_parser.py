"""Match parser — parse downloaded match HTML and store structured data in ClickHouse.

Usage:
    python -m src.match_parser                  # parse all downloaded matches
    python -m src.match_parser --limit 100      # limit to 100 matches
    python -m src.match_parser -v               # verbose logging
"""

import html as html_module
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.db_client import Database
from src.fetcher import PageFetcher
from src.tournament_resolver import TournamentResolver
from src.bracket_fetcher import BracketFetcher

logger = logging.getLogger(__name__)


@dataclass
class MapResult:
    """Result for a single map in a match."""
    map_name: str
    player1_score: int
    player2_score: int
    player1_name: str = ""
    player2_name: str = ""
    map_id: int = 0
    image: str = ""  # PlusForward map image path (e.g. /files/images/maps/11_....jpg)


@dataclass
class MatchDetail:
    """Complete match detail data."""
    match_id: int
    player1_id: int
    player2_id: int
    player1_name: str
    player2_name: str
    player1_country: str
    player2_country: str
    player1_score: int
    player2_score: int
    winner_id: int
    game_name: str
    game_category_id: int
    match_format: str
    tournament_id: int
    tournament_name: str
    stage_name: str
    played_at: datetime
    maps: list[MapResult] = field(default_factory=list)


class MatchDetailParser:
    """Parse match detail HTML pages."""

    def parse(self, html: str, match_id: int) -> Optional[MatchDetail]:
        """Parse a match detail page."""
        try:
            return self._parse(html, match_id)
        except Exception as e:
            logger.error(f"parse failed: {match_id}: {e}")
            return None

    def parse_with_reason(self, html: str, match_id: int) -> tuple[Optional[MatchDetail], str]:
        """Parse a post, returning (detail, reason).

        On success reason is ''. On failure reason is a short classifier:
          - 'not a match'  : page has no match area (news/VOD/forum/etc.)
          - 'invalid'      : page is a deleted/invalid post
          - 'team format'  : a match but not 1v1 duel (2v2, TDM, CTF, ...)
          - 'parse error'  : a duel match that failed to parse fully
        """
        try:
            detail = self._parse(html, match_id)
            if detail is not None:
                return detail, ""
            return None, self._classify_skip(html, match_id)
        except Exception as e:
            logger.error(f"parse failed: {match_id}: {e}")
            return None, "parse error"

    def _classify_skip(self, html: str, match_id: int) -> str:
        """Classify why a post failed to parse into a MatchDetail."""
        html = html or ""
        # Deleted post: title is exactly 'deleted | Plus Forward'.
        m = re.search(r"<title>([^<]*)</title>", html, re.IGNORECASE)
        if m and m.group(1).strip().lower().startswith("deleted"):
            return "invalid"
        # No match area at all → not a match post (news, VOD, forum, ...).
        if not re.search(r'<div class="match">', html):
            return "not a match"
        # Has a match area but isn't a 1v1 duel (team format).
        m = re.search(r'<div class="match">(.*?)</div><!--posthits=', html, re.DOTALL)
        if not m:
            m = re.search(r'<div class="match">(.*?)</div>\s*<div class="sharelinks">', html, re.DOTALL)
        if m:
            info = self._parse_match_info(m.group(1))
            fmt = info.get("format", "").lower()
            if fmt and "duel" not in fmt and "1v1" not in fmt:
                return "team format"
            # Has a match area but no scores → upcoming/live match, not yet played.
            if self._parse_scores(m.group(1)) is None:
                return "not played"
        return "parse error"

    def _parse(self, html: str, match_id: int) -> Optional[MatchDetail]:
        """Internal parse implementation."""
        # Find the main match area
        match_area = re.search(
            r'<div class="match">(.*?)</div><!--posthits=',
            html,
            re.DOTALL,
        )
        if not match_area:
            match_area = re.search(
                r'<div class="match">(.*?)</div>\s*<div class="sharelinks">',
                html,
                re.DOTALL,
            )
        if not match_area:
            logger.warning(f"no match area: {match_id}")
            return None

        content = match_area.group(1)

        # Parse match info first (need format to filter)
        info = self._parse_match_info(content)

        # Only parse 1v1 duels — skip team formats (TDM, CTF, Team 6v6, etc.)
        match_format = info.get("format", "").lower()
        is_duel = "duel" in match_format and "team" not in match_format
        is_1v1 = "1v1" in match_format
        if not is_duel and not is_1v1:
            # Non-duel matches are expected and common — don't log per-match
            return None

        # Parse players
        players = self._parse_players(content)
        if len(players) < 2:
            logger.warning(f"no players found: {match_id}")
            return None

        p1, p2 = players[0], players[1]

        # Parse scores
        scores = self._parse_scores(content)
        if scores is None:
            logger.warning(f"no scores: {match_id}")
            return None

        # Parse category_id from player link URLs as fallback
        if not info.get("category_id", 0):
            info["category_id"] = self._parse_category_from_links(content)

        # Parse maps (m_detailed is outside the <div class="match"> area)
        maps = self._parse_maps(html)

        # Determine winner. Prefer plusforward's explicit win marker (class='win'
        # on the winner's score div — present even at 0:0 for forfeits/walkovers).
        # Fall back to score comparison; if scores are equal and no marker, it's
        # a draw with no winner (winner_id = 0).
        win_marker = self._parse_win_marker(content)
        if win_marker == 1:
            winner_id = p1["id"]
        elif win_marker == 2:
            winner_id = p2["id"]
        elif scores[0] > scores[1]:
            winner_id = p1["id"]
        elif scores[1] > scores[0]:
            winner_id = p2["id"]
        else:
            winner_id = 0

        # Normalize a marked winner's score to 1:0 in the winner's favor.
        # PlusForward can mark a winner (forfeit/walkover) while the raw score
        # reads 0:0; storing 1:0 makes every downstream consumer (match page,
        # head-to-head, ratings) see a meaningful win instead of a phantom draw.
        if winner_id and scores[0] == scores[1]:
            if winner_id == p1["id"]:
                scores = (1, 0)
            else:
                scores = (0, 1)

        return MatchDetail(
            match_id=match_id,
            player1_id=p1["id"],
            player2_id=p2["id"],
            player1_name=p1["name"],
            player2_name=p2["name"],
            player1_country=p1["country"],
            player2_country=p2["country"],
            player1_score=scores[0],
            player2_score=scores[1],
            winner_id=winner_id,
            game_name=info.get("game", ""),
            game_category_id=info.get("category_id", 0),
            match_format=info.get("format", ""),
            tournament_id=info.get("tournament_id", 0),
            tournament_name=info.get("tournament", ""),
            stage_name=info.get("stage", ""),
            played_at=info.get("date", datetime.now()),
            maps=maps,
        )

    def _parse_players(self, content: str) -> list[dict]:
        """Parse player information from match content.

        Each player dict includes: id, name, country.

        The country flag lives inside the same .m_name_flag block as the player
        link, so we parse per-block: for each m_name_flag, grab the player link
        and the flag within that same block. This avoids grabbing unrelated
        flags from page navigation (the old code took the first two flags on
        the whole page, which were usually header/nav flags, not the players').
        """
        players = []

        # A .m_name_flag block contains the player link and their country flag.
        block_pattern = re.compile(
            r'<div class="m_name_flag">(.*?)</div>\s*</div>',
            re.DOTALL | re.IGNORECASE,
        )
        link_pattern = re.compile(
            r'<a href="/player/(\d+)/([^/"]+)/\d+/[^"]*/">\s*([^<]+)</a>',
            re.IGNORECASE,
        )
        flag_pattern = re.compile(r's_flag\s+s_flag-([a-z0-9]{2,3})', re.IGNORECASE)

        for block in block_pattern.finditer(content):
            block_html = block.group(1)
            lm = link_pattern.search(block_html)
            if not lm:
                continue
            player_id = int(lm.group(1))
            display_name = html_module.unescape(lm.group(3).strip())
            fm = flag_pattern.search(block_html)
            country = fm.group(1).lower() if fm else ""
            players.append({
                "id": player_id,
                "name": display_name,
                "country": country,
            })
            if len(players) >= 2:
                break

        return players

    def _parse_scores(self, content: str) -> Optional[tuple[int, int]]:
        """Parse match scores."""
        score_pattern = re.compile(
            r'<div class="res"><div(?P<cls>\s+class="(?:win|loss)")?>(?P<score>-?\d+)</div></div>'
        )
        scores = [(m.group('score'), m.group('cls')) for m in score_pattern.finditer(content)]
        if len(scores) >= 2:
            return (int(scores[0][0]), int(scores[1][0]))
        return None

    def _parse_win_marker(self, content: str) -> int:
        """Return 1 if plusforward marks player 1 as the winner via class='win',
        2 if player 2, else 0 (no explicit marker / draw).

        PlusForward marks the winner with class='win' on the winner's score div,
        even when the score is 0:0 (e.g. forfeit / walkover). We honor that
        explicit marker when present; otherwise the caller falls back to score
        comparison.
        """
        score_pattern = re.compile(
            r'<div class="res"><div(?P<cls>\s+class="(?:win|loss)")?>(?P<score>-?\d+)</div></div>'
        )
        items = [m.group('cls') or '' for m in score_pattern.finditer(content)]
        if len(items) >= 2:
            w1 = 'class="win"' in items[0]
            w2 = 'class="win"' in items[1]
            if w1 and not w2:
                return 1
            if w2 and not w1:
                return 2
        return 0

    def _parse_match_info(self, content: str) -> dict:
        """Parse match metadata (date, tournament, format, etc.)."""
        info = {}

        # Game and format
        desc_pattern = re.compile(
            r'<div class="title">Description</div><div>([^<]+)<br/>([^<]+)</div>'
        )
        desc = desc_pattern.search(content)
        if desc:
            info["game"] = desc.group(1).strip()
            info["format"] = desc.group(2).strip()

        # Category ID from the pfcat icon
        cat_pattern = re.compile(r'class="pfcat\s+pfcat-(\d+)"')
        cat = cat_pattern.search(content)
        if cat:
            info["category_id"] = int(cat.group(1))

        # Date
        date_pattern = re.compile(
            r'<div class="title">Date</div><div class="date"><div>([^<]+)</div><div>([^<]+)</div></div>'
        )
        date_match = date_pattern.search(content)
        if date_match:
            time_str = date_match.group(1)
            date_str = date_match.group(2)
            info["date"] = self._parse_detail_datetime(date_str, time_str)

        # Tournament
        tournament_pattern = re.compile(
            r'<div class="title">Tournament</div><div><a href="/post/(\d+)/[^"]+/"[^>]*>([^<]+)</a>'
        )
        t_match = tournament_pattern.search(content)
        if t_match:
            info["tournament_id"] = int(t_match.group(1))
            info["tournament"] = html_module.unescape(t_match.group(2).strip())

        # Stage
        stage_pattern = re.compile(
            r'<div class="title">Tournament</div>.*?<div>([^<]+)</div></div></div>',
            re.DOTALL,
        )
        s_match = stage_pattern.search(content)
        if s_match:
            stage = html_module.unescape(s_match.group(1).strip())
            if stage:
                info["stage"] = stage

        return info

    def _parse_maps(self, content: str) -> list[MapResult]:
        """Parse map results from match content.

        Map data lives in <div class="m_detailed"> tables outside the main
        <div class="match"> area. Not all matches have map data.
        """
        maps: list[MapResult] = []

        detailed_match = re.search(
            r'<div class="m_detailed">(.*?)</div>\s*<script',
            content,
            re.DOTALL,
        )
        if not detailed_match:
            return maps

        detailed = detailed_match.group(1)

        # Map name cell is either a known map (span with data-name) or an
        # unknown map shown as a bare "?" (td with title="unknown map").
        # Match both so the ? maps are still captured with their scores.
        # The known-map span also carries data-image="/files/images/maps/{id}_..."
        # from which we extract the PlusForward map ID (0 if absent).
        map_cell = (
            r'(?:'
            r'<td class="map">\s*<span[^>]*data-name="([^"]+)"[^>]*>[^<]*</span>\s*</td>'
            r'|<td class="map"[^>]*>\s*([?])\s*</td>'
            r')'
        )
        row_pattern = re.compile(
            r'<tr>\s*'
            + map_cell + r'\s*'
            r'<td class="mp_left[^"]*">([^<]*)</td>\s*'
            r'<td class="score_value[^"]*">(\d+)</td>\s*'
            r'<td class="score_value[^"]*">(\d+)</td>\s*'
            r'<td class="mp_right[^"]*">([^<]*)</td>\s*'
            r'</tr>',
            re.IGNORECASE | re.DOTALL,
        )

        for m in row_pattern.finditer(detailed):
            # Map name: group(1) = known map, group(2) = unknown "?"
            map_name = (m.group(1) or m.group(2) or "?").strip()
            p1_name = m.group(3).strip()
            p1_score = int(m.group(4))
            p2_score = int(m.group(5))
            p2_name = m.group(6).strip()
            # Map ID + image: extract from the data-image attribute of this
            # row's span (e.g. /files/images/maps/11_bloodrun.jpg -> id 11).
            map_id = 0
            image = ""
            span = re.search(r'<td class="map">\s*<span[^>]*>', m.group(0))
            if span:
                img = re.search(r'data-image="([^"]*)/maps/(\d+)_[^"]*"', span.group(0))
                if img:
                    image = img.group(1) + "/maps/" + img.group(2) + "_"
                    map_id = int(img.group(2))
                    # Recover the full image path from the span's data-image.
                    full = re.search(r'data-image="([^"]+)"', span.group(0))
                    if full:
                        image = full.group(1)
            maps.append(MapResult(
                map_name=map_name,
                player1_score=p1_score,
                player2_score=p2_score,
                player1_name=p1_name,
                player2_name=p2_name,
                map_id=map_id,
                image=image,
            ))

        return maps

    def _parse_category_from_links(self, content: str) -> int:
        """Extract game category_id from player link URLs as fallback."""
        for m in re.finditer(r'/player/\d+/[^/]+/(\d+)/[^/]+/', content):
            cat = int(m.group(1))
            if cat > 0:
                return cat
        return 0

    @staticmethod
    def _parse_detail_datetime(date_str: str, time_str: str) -> datetime:
        """Parse date like '1st August 2026' + '20:30 UTC'."""
        date_clean = re.sub(r'(\d+)(?:st|nd|rd|th)', r'\1', date_str)
        time_clean = time_str.replace(" UTC", "").strip()
        combined = f"{date_clean} {time_clean}"
        return datetime.strptime(combined, "%d %B %Y %H:%M")


def _is_tournament_in_progress(db, tournament_id: int) -> bool:
    """True if the tournament is currently in progress (not yet over).

    A tournament is over if its schedule_end has passed OR it has published
    final rankings with real players. Otherwise it's still in progress, so we
    refresh it on each new match.
    """
    try:
        det = db.get_tournament_details(tournament_id)
        if not det:
            return True
        end = det.get("schedule_end")
        if end and end < datetime.utcnow():
            return False
        rankings = det.get("rankings") or ""
        if isinstance(rankings, str):
            try:
                rankings = json.loads(rankings or "[]")
            except Exception:
                rankings = []
        # Still in progress until EVERY ranked position has a real player
        # (complete standings). Partial rankings = event still running.
        if rankings and all(r.get("player_name") for r in rankings):
            return False
        return True
    except Exception:
        return True


def store_parsed_match(db: Database, detail: MatchDetail, resolver: TournamentResolver = None,
                       bracket_fetcher=None):
    """Store parsed match data into ClickHouse tables.

    Inserts into: players, tournaments, matches, match_maps.
    Uses ReplacingMergeTree dedup for idempotency.

    Args:
        db: Database instance.
        detail: Parsed match details.
        resolver: Optional TournamentResolver instance. If provided, resolves
            tournament tier from PlusForward. If None, tier stays empty.
        bracket_fetcher: Optional BracketFetcher instance. If provided, fetches
            the tournament's bracket (Toornament/shambler) once the tournament
            has been resolved, if it has a bracket source and none is stored.
    """
    # Upsert players
    db.upsert_player(detail.player1_id, detail.player1_name, detail.player1_country)
    db.upsert_player(detail.player2_id, detail.player2_name, detail.player2_country)

    # Record historical name spellings (aliases) for each player
    db.record_aliases(detail.player1_id, [detail.player1_name])
    db.record_aliases(detail.player2_id, [detail.player2_name])

    # Upsert game (game_id = PlusForward category ID, from pfcat icon)
    db.upsert_game(detail.game_category_id, detail.game_name)

    # Upsert tournament (if present)
    if detail.tournament_id > 0:
        # For in-progress tournaments, force a fresh download + re-parse of the
        # tournament page (and bracket) on each new match, so standings and
        # brackets stay current as results roll in. Once the event is over we
        # fall back to the cached copy.
        force_refresh = _is_tournament_in_progress(db, detail.tournament_id)
        if resolver:
            # resolver.resolve() downloads the page and upserts name + tier +
            # raw_html + all parsed metadata. Do NOT call upsert_tournament
            # again here — it would overwrite raw_html with "" and clobber
            # the cached page.
            resolver.resolve(detail.tournament_id, force=force_refresh)
        else:
            # No resolver — just ensure the tournament name is stored.
            # Use empty raw_html only if the tournament doesn't exist yet,
            # to avoid clobbering previously stored HTML.
            existing = db.get_tournament_html(detail.tournament_id)
            if not existing:
                db.upsert_tournament(detail.tournament_id, detail.tournament_name, "")

        # Fetch the tournament's bracket (Toornament/shambler) if it has a
        # bracket source and none is stored yet (or force-refresh for live
        # events). Idempotent — skips if already fetched unless force.
        if bracket_fetcher is not None and detail.tournament_id > 0:
            try:
                bracket_fetcher.fetch_for_tournament_if_needed(
                    detail.tournament_id, force=force_refresh)
            except Exception as e:
                logger.warning(f"bracket fetch failed for tournament {detail.tournament_id}: {e}")

    # Insert match
    db.insert_match(detail)

    # Insert maps (if any)
    db.insert_match_maps(detail.match_id, detail.maps, detail.played_at)

    # Populate the canonical maps table (map_id -> name/image/game) from the
    # parsed map data, so the player map chart and tournament map plaques have
    # names/images. ReplacingMergeTree dedups by map_id.
    if detail.maps:
        for mp in detail.maps:
            if mp.map_id > 0 and mp.map_name and mp.map_name != "?":
                db.upsert_map(mp.map_id, mp.map_name, mp.image, detail.game_name)


def _parse_worker_init():
    """Initialize thread-local resources (one per worker thread).

    Each thread gets its own Database connection, MatchDetailParser, PageFetcher,
    and TournamentResolver. This avoids sharing rate-limited fetchers and DB connections
    across threads.
    """
    import threading
    if not hasattr(_parse_worker_init, "_local"):
        _parse_worker_init._local = threading.local()
    return _parse_worker_init._local


def _is_tournament_post(html: str) -> bool:
    """True if the page is a tournament page (has tournament structure)."""
    return bool(html) and "postinnercontent" in html and "tour_info" in html


def _parse_post(db, parser, resolver, bracket_fetcher, post_id: int, raw_html: str) -> tuple[bool, str]:
    """Parse a single downloaded post (match or tournament) and store it.

    Returns (success, reason). On success reason is ''. On failure reason is a
    short classifier ('not a match', 'team format', 'invalid', 'parse error').
    """
    # Tournament post → resolve via TournamentResolver (stores metadata + HTML).
    if _is_tournament_post(raw_html):
        try:
            resolver.resolve(post_id)
            db.raw_post_mark(post_id, "parsed")
            return True, ""
        except Exception as e:
            logger.error(f"tournament resolve failed: {post_id}: {e}")
            db.raw_post_mark(post_id, "skipped", "parse error")
            return False, "parse error"

    # Match post → parse via MatchDetailParser.
    detail, reason = parser.parse_with_reason(raw_html, post_id)
    if detail is None:
        db.raw_post_mark(post_id, "skipped", reason or "parse error")
        return False, reason or "parse error"

    try:
        store_parsed_match(db, detail, resolver, bracket_fetcher)
        db.raw_post_mark(post_id, "parsed")
        return True, ""
    except Exception as e:
        logger.error(f"store failed: {post_id}: {e}")
        db.raw_post_mark(post_id, "skipped", "parse error")
        return False, "parse error"


def _parse_worker(task: tuple, preloaded_tiers: dict = None) -> tuple[int, bool, str]:
    """Parse and store a single match in a worker thread.

    Each thread has its own Database, PageFetcher, TournamentResolver, and parser.
    This parallelizes both CPU-bound parsing and I/O-bound tier resolution.

    Args:
        task: (match_id, played_at, raw_html) tuple.
        preloaded_tiers: Shared dict of tournament_id -> tier (pre-loaded from DB).

    Returns:
        (match_id, success, error_message_or_empty)
    """
    local = _parse_worker_init()
    if not hasattr(local, "db"):
        local.db = Database()
        local.parser = MatchDetailParser()
        local.fetcher = PageFetcher()
        local.resolver = TournamentResolver(local.db, local.fetcher)
        local.bracket_fetcher = BracketFetcher(local.db)
        if preloaded_tiers:
            local.resolver.preload_tiers(preloaded_tiers)

    match_id, raw_html = task
    try:
        ok, reason = _parse_post(local.db, local.parser, local.resolver,
                                 local.bracket_fetcher, match_id, raw_html)
        return match_id, ok, reason
    except Exception as e:
        return match_id, False, str(e)


def parse_all_matches(limit: int = 0, workers: int = 0) -> tuple[int, int]:
    """Parse all downloaded matches and store results in ClickHouse.

    Uses a thread pool to parallelize HTML parsing (CPU-bound) and tier
    resolution (I/O-bound). Each worker thread has its own Database,
    PageFetcher, and TournamentResolver instance.

    Args:
        limit: Maximum matches to parse (0 = unlimited).
        workers: Number of parser threads (0 = PARSER_WORKERS from config).

    Returns:
        (success_count, failure_count)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from config import PARSER_WORKERS

    if workers <= 0:
        workers = PARSER_WORKERS

    db = Database()

    # Get all posts downloaded but not yet parsed (status 'downloaded').
    rows = db.raw_post_get_downloaded(limit)
    total = len(rows)
    db.close()

    if total == 0:
        logger.debug("no posts to parse")
        return 0, 0

    logger.info(f"{total} to parse | {workers}w")

    success = 0
    failure = 0
    start_time = time.time()

    # Log progress at ~10% intervals or every 2000 items
    progress_interval = max(total // 10, 2000) if total > 1000 else 100

    if workers <= 1:
        # Single-threaded: simple interruptible loop
        db = Database()
        parser = MatchDetailParser()
        fetcher = PageFetcher()
        resolver = TournamentResolver(db, fetcher)
        try:
            for i, (match_id, raw_html) in enumerate(rows, 1):
                ok, reason = _parse_post(db, parser, resolver, None, match_id, raw_html)
                if ok:
                    success += 1
                else:
                    failure += 1

                if i % progress_interval == 0 or i == total:
                    elapsed = time.time() - start_time
                    rate = i / elapsed if elapsed > 0 else 0
                    pct = i * 100 // total
                    logger.debug(f"{i}/{total} ({pct}%) — {success} ok, {failure} skip, {rate:.1f}/s")
        finally:
            db.close()
    else:
        # Multi-threaded: ThreadPoolExecutor
        # Each worker handles parse + tier resolve + store with its own DB connection.
        # Pre-load all known tournament tiers from DB so workers don't hammer PlusForward.
        db = Database()
        preloaded_tiers = db.get_tournament_tiers_with_html()
        db.close()
        logger.debug(f"preloading {len(preloaded_tiers)} tiers into {workers}w")

        try:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(_parse_worker, (mid, html), preloaded_tiers): mid
                           for mid, html in rows}

                for i, future in enumerate(as_completed(futures), 1):
                    mid = futures[future]
                    try:
                        match_id, ok, err = future.result()
                        if ok:
                            success += 1
                        else:
                            failure += 1
                            # Expected skips (not a match / team format / invalid)
                            # are common — don't log them as warnings.
                            if err and err not in ("not a match", "team format", "invalid", "not played", "parse error"):
                                logger.warning(f"parse failed: {match_id}: {err}")
                    except Exception as e:
                        logger.error(f"parse error: {mid}: {e}")
                        failure += 1

                    if i % progress_interval == 0 or i == total:
                        elapsed = time.time() - start_time
                        rate = i / elapsed if elapsed > 0 else 0
                        pct = i * 100 // total
                        logger.debug(f"{i}/{total} ({pct}%) — {success} ok, {failure} skip, {rate:.1f}/s")
        finally:
            # Worker DB connections are closed when threads exit.
            # For long-running processes, we rely on thread cleanup.
            pass

    elapsed = time.time() - start_time
    logger.info(f"done: {success} ok, {failure} fail, {total} total, {elapsed:.0f}s")
    return success, failure


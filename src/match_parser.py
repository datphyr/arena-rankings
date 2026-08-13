"""Match parser — parse downloaded match HTML and store structured data in ClickHouse.

Usage:
    python -m src.match_parser                  # parse all downloaded matches
    python -m src.match_parser --limit 100      # limit to 100 matches
    python -m src.match_parser -v               # verbose logging
"""

import html as html_module
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.db_client import Database
from src.fetcher import PageFetcher
from src.tournament_resolver import TournamentResolver

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

        # Determine winner. Only a strictly higher score is a win; equal
        # scores (0-0, 1-1, ...) are a draw with no winner (winner_id = 0).
        if scores[0] > scores[1]:
            winner_id = p1["id"]
        elif scores[1] > scores[0]:
            winner_id = p2["id"]
        else:
            winner_id = 0

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
        """Parse player information from match content."""
        players = []

        player_pattern = re.compile(
            r'<a href="/player/(\d+)/([^/]+)/\d+/[^/]+/">\s*([^<]+)</a>',
            re.IGNORECASE,
        )

        for m in player_pattern.finditer(content):
            player_id = int(m.group(1))
            display_name = m.group(3).strip()
            players.append({
                "id": player_id,
                "name": html_module.unescape(display_name),
                "country": "",
            })

        # Find country flags
        flag_pattern = re.compile(
            r'<span class="s_flag\s+s_flag-([a-z]{2})"[^>]*title="([^"]+)"'
        )
        flags = flag_pattern.findall(content)
        for i, (code, country) in enumerate(flags[:2]):
            if i < len(players):
                players[i]["country"] = code

        return players

    def _parse_scores(self, content: str) -> Optional[tuple[int, int]]:
        """Parse match scores."""
        score_pattern = re.compile(
            r'<div class="res"><div(?:\s+class="(?:win|loss)")?>(-?\d+)</div></div>'
        )
        scores = score_pattern.findall(content)
        if len(scores) >= 2:
            return (int(scores[0]), int(scores[1]))
        return None

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
            # Map ID: extract from the data-image attribute of this row's span.
            # The span is the first <span ...> inside the <td class="map">.
            map_id = 0
            span = re.search(r'<td class="map">\s*<span[^>]*>', m.group(0))
            if span:
                img = re.search(r'data-image="[^"]*/maps/(\d+)_[^"]*"', span.group(0))
                if img:
                    map_id = int(img.group(1))
            maps.append(MapResult(
                map_name=map_name,
                player1_score=p1_score,
                player2_score=p2_score,
                player1_name=p1_name,
                player2_name=p2_name,
                map_id=map_id,
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


def store_parsed_match(db: Database, detail: MatchDetail, resolver: TournamentResolver = None):
    """Store parsed match data into ClickHouse tables.

    Inserts into: players, tournaments, matches, match_maps.
    Uses ReplacingMergeTree dedup for idempotency.

    Args:
        db: Database instance.
        detail: Parsed match details.
        resolver: Optional TournamentResolver instance. If provided, resolves
            tournament tier from PlusForward. If None, tier stays empty.
    """
    # Upsert players
    db.upsert_player(detail.player1_id, detail.player1_name, detail.player1_country)
    db.upsert_player(detail.player2_id, detail.player2_name, detail.player2_country)

    # Upsert tournament (if present)
    if detail.tournament_id > 0:
        if resolver:
            # resolver.resolve() downloads the page and upserts name + tier +
            # raw_html + all parsed metadata. Do NOT call upsert_tournament
            # again here — it would overwrite raw_html with "" and clobber
            # the cached page.
            resolver.resolve(detail.tournament_id)
        else:
            # No resolver — just ensure the tournament name is stored.
            # Use empty raw_html only if the tournament doesn't exist yet,
            # to avoid clobbering previously stored HTML.
            existing = db.get_tournament_html(detail.tournament_id)
            if not existing:
                db.upsert_tournament(detail.tournament_id, detail.tournament_name, "")

    # Insert match
    db.insert_match(detail)

    # Insert maps (if any)
    db.insert_match_maps(detail.match_id, detail.maps, detail.played_at)


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
        if preloaded_tiers:
            local.resolver.preload_tiers(preloaded_tiers)

    match_id, orig_played_at, raw_html = task
    try:
        detail = local.parser.parse(raw_html, match_id)
        if detail is None:
            # INSERT new row with failed status, same played_at so ReplacingMergeTree dedups
            local.db.client.execute(
                "INSERT INTO match_registry "
                "(match_id, played_at, raw_html, status) VALUES",
                [(match_id, orig_played_at, raw_html, "failed")],
            )
            return match_id, False, "parse failed"

        # Store structured data first (tier resolution happens here)
        store_parsed_match(local.db, detail, local.resolver)
        # Only mark as parsed if store succeeded
        local.db.client.execute(
            "INSERT INTO match_registry "
            "(match_id, played_at, raw_html, status) VALUES",
            [(match_id, detail.played_at, raw_html, "parsed")],
        )
        return match_id, True, ""
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

    # Get all matches with raw_html downloaded but not yet parsed
    query = (
        "SELECT match_id, played_at, raw_html FROM match_registry FINAL "
        "WHERE status = 'downloaded' "
        "ORDER BY played_at DESC"
    )
    if limit > 0:
        query += f" LIMIT {limit}"

    rows = db.client.execute(query)
    total = len(rows)
    db.close()

    if total == 0:
        logger.debug("no matches to parse")
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
            for i, (match_id, orig_played_at, raw_html) in enumerate(rows, 1):
                detail = parser.parse(raw_html, match_id)
                if detail is None:
                    failure += 1
                    db.client.execute(
                        "INSERT INTO match_registry "
                        "(match_id, played_at, raw_html, status) VALUES",
                        [(match_id, orig_played_at, raw_html, "failed")],
                    )
                else:
                    try:
                        store_parsed_match(db, detail, resolver)
                        db.client.execute(
                            "INSERT INTO match_registry "
                            "(match_id, played_at, raw_html, status) VALUES",
                            [(match_id, detail.played_at, raw_html, "parsed")],
                        )
                        success += 1
                    except Exception as e:
                        logger.error(f"store failed: {match_id}: {e}")
                        db.client.execute(
                            "INSERT INTO match_registry "
                            "(match_id, played_at, raw_html, status) VALUES",
                            [(match_id, orig_played_at, raw_html, "failed")],
                        )
                        failure += 1

                if i % progress_interval == 0 or i == total:
                    elapsed = time.time() - start_time
                    rate = i / elapsed if elapsed > 0 else 0
                    pct = i * 100 // total
                    logger.debug(f"{i}/{total} ({pct}%) — {success} ok, {failure} fail, {rate:.1f}/s")
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
                futures = {executor.submit(_parse_worker, (mid, played_at, html), preloaded_tiers): mid
                           for mid, played_at, html in rows}

                for i, future in enumerate(as_completed(futures), 1):
                    mid = futures[future]
                    try:
                        match_id, ok, err = future.result()
                        if ok:
                            success += 1
                        else:
                            failure += 1
                            if err and err != "parse failed":
                                logger.warning(f"parse failed: {match_id}: {err}")
                    except Exception as e:
                        logger.error(f"parse error: {mid}: {e}")
                        failure += 1

                    if i % progress_interval == 0 or i == total:
                        elapsed = time.time() - start_time
                        rate = i / elapsed if elapsed > 0 else 0
                        pct = i * 100 // total
                        logger.debug(f"{i}/{total} ({pct}%) — {success} ok, {failure} fail, {rate:.1f}/s")
        finally:
            # Worker DB connections are closed when threads exit.
            # For long-running processes, we rely on thread cleanup.
            pass

    elapsed = time.time() - start_time
    logger.info(f"done: {success} ok, {failure} fail, {total} total, {elapsed:.0f}s")
    return success, failure


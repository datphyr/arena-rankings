"""Tournament resolution from PlusForward tournament pages.

Fetches tournament pages and extracts:
  - tier (premier/major/minor) from CSS classes / title heuristics
  - tournament metadata (game, prize money, formats, maplist, final rankings,
    schedule) from the .tour_info and .tour_rankings blocks
Stores raw HTML and the parsed metadata in the tournaments table.

Usage:
    from src.tournament_resolver import TournamentResolver
    resolver = TournamentResolver(db, fetcher)
    tier = resolver.resolve(tournament_id)  # "premier", "major", "minor", or ""
"""

import json
import logging
import random
import re
import time
import html as _html
from datetime import datetime
from typing import Optional

from config import BASE_URL, RETRY_BACKOFF
from src.fetcher import PageFetcher

logger = logging.getLogger(__name__)

# Default when tier can't be resolved.
DEFAULT_TIER = ""


class TournamentResolver:
    """Resolve tournament tier + metadata by fetching and parsing the tournament page.

    Uses ClickHouse for HTML caching — raw HTML is stored in raw_posts
    (post_id = tournament_id). Parsed metadata is stored in the tournaments row.
    """

    # Class-level stats for observability.
    cache_hits: int = 0
    db_hits: int = 0
    network_fetches: int = 0
    failures: int = 0
    parsed_details: int = 0

    def __init__(self, db, fetcher: PageFetcher = None):
        """
        Args:
            db: Database instance for reading/writing tournament HTML + metadata.
            fetcher: PageFetcher instance. If None, creates one.
        """
        self._db = db
        self._fetcher = fetcher or PageFetcher()
        self._cache: dict[int, str] = {}  # in-memory: tournament_id -> tier
        self._preloaded = False

    def preload_tiers(self, tiers: dict[int, str]):
        """Pre-load known tiers into cache (avoids network fetches).

        Only preloads tiers for tournaments that also have raw_html in the DB,
        so the resolver won't skip fetching pages for tournaments missing HTML.

        Args:
            tiers: dict mapping tournament_id -> tier string. Should already
            be filtered to only tournaments with raw_html.
        """
        self._cache.update(tiers)
        self._preloaded = True
        if not getattr(TournamentResolver, '_preload_logged', False):
            TournamentResolver._preload_logged = True
            logger.debug(f"{len(tiers)} tiers preloaded (only those with raw_html)")

    def resolve(self, tournament_id: int, force: bool = False) -> str:
        """Return the tier for a tournament, using cache when possible.

        Args:
            tournament_id: PlusForward tournament id.
            force: if True, always re-download + re-parse the tournament page
                (used to refresh standings/brackets for in-progress events).
        """
        if tournament_id <= 0:
            return DEFAULT_TIER

        if force:
            tier = self._fetch_tier(tournament_id, force=True)
            self._cache[tournament_id] = tier
            return tier

        cached = self._cache.get(tournament_id)
        if cached is not None:
            TournamentResolver.cache_hits += 1
            return cached

        tier = self._fetch_tier(tournament_id)
        self._cache[tournament_id] = tier
        return tier

    @classmethod
    def log_stats(cls):
        """Log cache/db/network statistics."""
        logger.debug(
            f"tiers: {cls.cache_hits} cache, {cls.db_hits} db, "
            f"{cls.network_fetches} fetch, {cls.failures} fail, "
            f"{cls.parsed_details} details"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_tier(self, tournament_id: int, force: bool = False) -> str:
        """Resolve tournament tier, downloading and parsing as needed.

        Flow:
        1. **Check DB for parsed tier + raw_html**: if tier is already stored
           AND raw_html is cached, return tier. Both must be present — tier
           without HTML means a previous run clobbered the HTML.
        2. **Check DB for raw_html only**: if HTML is cached but tier wasn't
           parsed, parse tier + metadata from HTML, upsert, return tier.
        3. **Download**: no cached HTML — fetch from PlusForward, parse, upsert,
           return tier.

        When force=True, steps 1-2 are skipped and the page is always
        re-downloaded + re-parsed (to refresh standings/brackets for an
        in-progress tournament).
        """
        if not force:
            # 1. Check DB for already-parsed tier AND cached HTML.
            tier = self._db.get_tournament_tier(tournament_id)
            html = ""
            if tier:
                html = self._db.get_tournament_html(tournament_id)
                if html:
                    # Fast path: tier + html already stored. But if the name is
                    # empty (e.g. parsed before name extraction existed), recover
                    # it from the cached HTML rather than leaving it blank.
                    name_rows = self._db.client.execute(
                        "SELECT name FROM tournaments FINAL WHERE tournament_id = %(t)s",
                        {"t": tournament_id},
                    )
                    existing_name = name_rows[0][0] if name_rows else ""
                    if not existing_name:
                        parsed_name = self._parse_name(html)
                        if parsed_name:
                            # Preserve existing metadata; only update the name.
                            det = self._db.get_tournament_details(tournament_id) or {}
                            self._db.upsert_tournament(
                                tournament_id, parsed_name, tier,
                                game=det.get("game", ""),
                                prize_money=det.get("prize_money", ""),
                                tourney_format=det.get("tourney_format", ""),
                                match_format=det.get("match_format", ""),
                                schedule_start=det.get("schedule_start"),
                                schedule_end=det.get("schedule_end"),
                                maplist=det.get("maplist") or [],
                                rankings=det.get("rankings", "[]"),
                            )
                    TournamentResolver.db_hits += 1
                    return tier
                # Tier exists but raw_html is missing — fall through to download.
                # This happens when a previous run clobbered the HTML (the
                # match_parser.py:339 bug that's now fixed).
                logger.debug(f"tournament {tournament_id}: tier={tier!r} but no raw_html, fetching")

            # 2. Check DB for cached HTML — parse tier + metadata from it.
            if not html:
                html = self._db.get_tournament_html(tournament_id)
            if html:
                TournamentResolver.db_hits += 1
                return self._resolve_from_html(html, tournament_id)

        # 3. Fetch from network with infinite retries.
        #    Between retries, check DB — another worker may store HTML
        #    while we were waiting.
        url = f"{BASE_URL}/post/{tournament_id}/"
        TournamentResolver.network_fetches += 1
        html = self._fetch_with_db_checks(url, tournament_id)
        if html is None:
            # Should not happen with infinite retries, but guard anyway.
            TournamentResolver.failures += 1
            logger.error(f"tier fetch failed: {tournament_id} (retries exhausted)")
            return DEFAULT_TIER

        # We got HTML — parse and store.
        return self._resolve_from_html(html, tournament_id)

    def _resolve_from_html(self, html: str, tournament_id: int) -> str:
        """Parse tier + tournament metadata from HTML and upsert to DB."""
        tier = self._parse_tier(html, tournament_id)
        details = self._parse_tournament_details(html)
        # Prefer the name parsed from THIS page; fall back to the existing DB
        # name only if the page has no name (avoids clobbering a known name
        # with empty when resolving a tournament page without a title).
        name = self._parse_name(html) or self._get_existing_name(tournament_id)
        # Store the raw page HTML in raw_posts (post_id = tournament_id).
        self._db.store_raw_post(tournament_id, html, "parsed")
        self._db.upsert_tournament(
            tournament_id, name, tier,
            game=details["game"], prize_money=details["prize_money"],
            tourney_format=details["tourney_format"], match_format=details["match_format"],
            schedule_start=details["schedule_start"], schedule_end=details["schedule_end"],
            maplist=details["maplist"], rankings=details["rankings"],
        )
        if details["rankings"] != "[]" or details["maplist"]:
            TournamentResolver.parsed_details += 1
        return tier

    @staticmethod
    def _parse_name(html: str) -> str:
        """Extract the tournament name from the page title.

        Format: <h1 class="posttitle"><a href="/post/123/...">Name</a></h1>
        """
        m = re.search(
            r'<h1 class="posttitle">\s*<a[^>]*href="/post/\d+/[^"]*">([^<]+)</a>',
            html, re.DOTALL)
        if m:
            return _html.unescape(re.sub(r'<[^>]+>', '', m.group(1)).strip())
        return ""

    def _get_existing_name(self, tournament_id: int) -> str:
        rows = self._db.client.execute(
            "SELECT name FROM tournaments FINAL WHERE tournament_id = %(t)s",
            {"t": tournament_id},
        )
        return rows[0][0] if rows else ""

    def _fetch_with_db_checks(self, url: str, tournament_id: int):
        """Fetch URL with infinite retries, checking DB between attempts.

        Returns:
            HTML string if fetched successfully or if another worker stored
                raw_html while we were retrying.
            None if all retries exhausted (should not happen with infinite).
        """
        attempt = 0
        while True:
            html = self._fetcher.fetch(url, max_retries=1)
            if html:
                return html

            # Fetch failed. Check if another worker resolved it while we were
            # trying — but only short-circuit if raw_html was also stored.
            html = self._db.get_tournament_html(tournament_id)
            if html:
                logger.debug(f"tournament {tournament_id} html stored by another worker")
                return html

            attempt += 1
            wait = min(RETRY_BACKOFF ** attempt, 60)
            wait += random.uniform(0, wait * 0.3)
            logger.debug(f"tournament {tournament_id}: fetch attempt {attempt} failed, retry in {wait:.1f}s")
            time.sleep(wait)

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_tier(self, html: str, tournament_id: int) -> str:
        """Extract tier from cached/fetched HTML.

        The sidebar calendar widget is global (identical on every page),
        so only the page's own main content title is meaningful.

        Strategies:
        1. Inner title keyword: <div class="title"><i class="pfcat-N"></i> Premier|Major ...
        2. Inner title exists but no tier keyword → minor
        """

        # Strategy 1: Main content title in postinnercontent.
        #   <div id="postinnercontent"><div class="title"><i class="pfcat-20"></i> Premier Quake Champions - TDM 2v2 Tournament </div>
        #   <div class="title"><i class="pfcat-3"></i> Major Quake Live - Duel Tournament </div>
        inner_title = re.search(
            r'<div id="postinnercontent">.*?<div class="title">\s*'
            r'<i class="pfcat-\d+"></i>\s*'
            r'(Premier|Major|Minor)\s',
            html, re.DOTALL | re.IGNORECASE,
        )
        if inner_title:
            return inner_title.group(1).lower()

        # Strategy 2: Inner title exists but no tier keyword → minor.
        #   Premier/major tournaments always have the keyword in the inner title.
        #   Minor tournaments never do — the title is just "Game - Format".
        inner_any = re.search(
            r'<div id="postinnercontent">.*?'
            r'<div class="title">\s*<i class="pfcat-\d+"></i>\s*\S',
            html, re.DOTALL,
        )
        if inner_any:
            return "minor"

        # Strategy 3: postinnercontent exists with a blockbegin title (non-standard
        #   tournament pages like UT Pro League, Beginners Cup). These are small
        #   community tournaments → minor.
        if 'postinnercontent' in html:
            logger.warning(f"tournament {tournament_id}: no standard title, using minor")
            return "minor"

        # Strategy 4: Page has no postinnercontent at all — non-standard page
        #   (stream posts, news entries that got into tournaments table).
        #   Default to minor for safety.
        logger.warning(f"tournament {tournament_id}: no postinnercontent, using minor")
        return "minor"

    def _parse_tournament_details(self, html: str) -> dict:
        """Extract tournament metadata from the page HTML.

        Returns a dict with keys: game, prize_money, tourney_format, match_format,
        schedule_start, schedule_end, maplist, rankings (JSON string). Any field
        that can't be found falls back to its empty default.
        """
        details = {
            "game": "",
            "prize_money": "",
            "tourney_format": "",
            "match_format": "",
            "schedule_start": None,
            "schedule_end": None,
            "maplist": [],
            "rankings": "[]",
        }

        # Main content block — the sidebar is global/identical, so scope all
        # extraction to the page's own postinnercontent.
        body = html
        m = re.search(r'<div id="postinnercontent">(.*?)(?:</div>\s*<div class="sidebar|$)', html, re.DOTALL)
        if m:
            body = m.group(1)

        # Game — from the inner title: <i class="pfcat-N"></i> [Tier] Game - Format
        game_m = re.search(
            r'<div class="title">\s*<i class="pfcat-\d+"></i>\s*(?:premier|major|minor\s+)?'
            r'([A-Za-z0-9 &\.\-]+?)(?:\s*[-–]\s*|\s*$)', body, re.DOTALL | re.IGNORECASE)
        if game_m:
            details["game"] = game_m.group(1).strip()

        # Info block fields: <div class="tc_title">Label</div><div>Value</div>
        def info_value(label):
            pat = re.compile(
                r'<div class="tc_title">' + re.escape(label) + r'</div>\s*<div[^>]*>(.*?)</div>',
                re.DOTALL | re.IGNORECASE)
            m = pat.search(body)
            if not m:
                return ""
            val = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            return val

        details["prize_money"] = info_value("Prizemoney")
        details["tourney_format"] = info_value("Tourney format")
        details["match_format"] = info_value("Match format")

        # Schedule: <div class="tc_title">Schedule</div><div>09 Aug 2026 - 16:00 UTC → 20:15 UTC</div>
        sched_m = re.search(
            r'<div class="tc_title">Schedule</div>\s*<div[^>]*>\s*'
            r'(\d{1,2}\s+\w+\s+\d{4})\s*[-–]\s*(\d{1,2}:\d{2})\s*UTC'
            r'(?:\s*<i[^>]*></i>\s*|\s*[-–>→]\s*|\s*)(\d{1,2}:\d{2})?\s*UTC?',
            body, re.DOTALL | re.IGNORECASE)
        if sched_m:
            try:
                start = datetime.strptime(sched_m.group(1), "%d %b %Y")
                day = start.strftime("%Y-%m-%d ")
                details["schedule_start"] = day + sched_m.group(2) + ":00"
                details["schedule_end"] = day + (sched_m.group(3) or sched_m.group(2)) + ":00"
            except ValueError:
                pass

        # Maplist: <span class="map_preview" ... data-name="Awoken">Awoken</span>
        maplist = re.findall(
            r'<span class="map_preview"[^>]*data-name="([^"]+)"', body)
        if maplist:
            details["maplist"] = maplist

        # Final rankings: .tour_rankings table rows
        #   <td class="position">1st</td> ... <a class="profile" href="/player/16947/...">Name</a> ... <td class="prizemoney">60 USD</td>
        rankings = []
        rank_rows = re.findall(
            r'<tr>.*?<td class="position">([^<]+)</td>.*?'
            r'<a class="profile" href="/player/(\d+)/[^"]*"[^>]*>(?:<span[^>]*></span>\s*)?([^<]+)</a>.*?'
            r'<td class="prizemoney">([^<]*)</td>',
            body, re.DOTALL)
        for pos, pid, pname, prize in rank_rows:
            rankings.append({
                "position": pos.strip(),
                "player_id": int(pid),
                "player_name": pname.strip(),
                "prize": prize.strip(),
            })
        if rankings:
            details["rankings"] = json.dumps(rankings, ensure_ascii=False)

        return details

"""Tier resolution from PlusForward tournament pages.

Fetches tournament pages and extracts tier (premier/major/minor) from CSS classes.
Stores raw HTML in the tournaments table (same pattern as match_registry/matches).

Usage:
    from src.tier_resolver import TierResolver
    resolver = TierResolver(db, fetcher)
    tier = resolver.resolve(tournament_id)  # "premier", "major", "minor", or ""
"""

import logging
import random
import re
import time
from typing import Optional

from config import BASE_URL, RETRY_BACKOFF
from src.fetcher import PageFetcher

logger = logging.getLogger(__name__)

# Default when tier can't be resolved.
DEFAULT_TIER = ""


class TierResolver:
    """Resolve tournament tier by fetching and parsing the tournament page.

    Uses ClickHouse for HTML caching — raw_html is stored in the tournaments table,
    same pattern as match_registry stores match HTML.
    """

    # Class-level stats for observability.
    cache_hits: int = 0
    db_hits: int = 0
    network_fetches: int = 0
    failures: int = 0

    def __init__(self, db, fetcher: PageFetcher = None):
        """
        Args:
            db: Database instance for reading/writing tournament HTML.
            fetcher: PageFetcher instance. If None, creates one.
        """
        self._db = db
        self._fetcher = fetcher or PageFetcher()
        self._cache: dict[int, str] = {}  # in-memory: tournament_id -> tier
        self._preloaded = False

    def preload_tiers(self, tiers: dict[int, str]):
        """Pre-load known tiers into cache (avoids network fetches).

        Args:
            tiers: dict mapping tournament_id -> tier string.
        """
        self._cache.update(tiers)
        self._preloaded = True
        # Only log once per process — workers call this independently
        if not getattr(TierResolver, '_preload_logged', False):
            TierResolver._preload_logged = True
            logger.debug(f"{len(tiers)} tiers preloaded")

    def resolve(self, tournament_id: int) -> str:
        """Return the tier for a tournament, using cache when possible.

        Always resolves — tier resolver is always enabled.
        """
        if tournament_id <= 0:
            return DEFAULT_TIER

        cached = self._cache.get(tournament_id)
        if cached is not None:
            TierResolver.cache_hits += 1
            return cached

        tier = self._fetch_tier(tournament_id)
        self._cache[tournament_id] = tier
        return tier

    @classmethod
    def log_stats(cls):
        """Log cache/db/network statistics."""
        logger.debug(f"tiers: {cls.cache_hits} cache, {cls.db_hits} db, {cls.network_fetches} fetch, {cls.failures} fail")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_tier(self, tournament_id: int) -> str:
        """Fetch tournament page and extract tier class.

        Strategy (in order):
        1. **DB tier check**: check if tier already resolved in tournaments table
        2. **ClickHouse HTML**: check if raw_html already stored in tournaments table
        3. **Network**: fetch from PlusForward with infinite retries, checking
           DB between attempts (another worker may resolve it while we wait)
        4. Parse the HTML for tier info using title heuristics
        """
        # 1. Check if tier is already resolved in DB (avoids network entirely).
        tier = self._check_db_tier(tournament_id)
        if tier:
            return tier

        # 2. Try ClickHouse HTML cache.
        html = self._db.get_tournament_html(tournament_id)
        if html:
            TierResolver.db_hits += 1
            return self._resolve_from_html(html, tournament_id)

        # 3. Fetch from network with infinite retries.
        #    Between retries, check DB — another worker may have resolved
        #    the tier while we were waiting.
        url = f"{BASE_URL}/post/{tournament_id}/"
        TierResolver.network_fetches += 1
        result = self._fetch_with_db_checks(url, tournament_id)
        if result is None:
            # Should not happen with infinite retries, but guard anyway.
            TierResolver.failures += 1
            logger.error(f"tier fetch failed: {tournament_id} (retries exhausted)")
            return DEFAULT_TIER

        # result is either HTML string (we fetched it) or a tier string
        # (another worker resolved it — return directly).
        if result in ("premier", "major", "minor"):
            return result

        # We got HTML — parse and store.
        return self._resolve_from_html(result, tournament_id)

    def _check_db_tier(self, tournament_id: int) -> str:
        """Check DB for already-resolved tier. Returns tier or ''."""
        tier_rows = self._db.client.execute(
            "SELECT tier FROM tournaments FINAL WHERE tournament_id = %(t)s",
            {"t": tournament_id},
        )
        if tier_rows and tier_rows[0][0]:
            TierResolver.db_hits += 1
            return tier_rows[0][0]
        return ""

    def _resolve_from_html(self, html: str, tournament_id: int) -> str:
        """Parse tier from HTML and upsert to DB."""
        tier = self._parse_tier(html, tournament_id)
        name_rows = self._db.client.execute(
            "SELECT name FROM tournaments FINAL WHERE tournament_id = %(t)s",
            {"t": tournament_id},
        )
        name = name_rows[0][0] if name_rows else ""
        self._db.upsert_tournament(tournament_id, name, tier, raw_html=html)
        return tier

    def _fetch_with_db_checks(self, url: str, tournament_id: int):
        """Fetch URL with infinite retries, checking DB between attempts.

        Returns:
            HTML string if fetched successfully.
            Tier string (e.g. "minor") if another worker resolved it while
                we were retrying.
            None if all retries exhausted (should not happen with infinite).
        """
        attempt = 0
        while True:
            html = self._fetcher.fetch(url, max_retries=1)
            if html:
                return html

            # Fetch failed. Check if another worker resolved the tier
            # (and stored HTML) while we were trying.
            tier = self._check_db_tier(tournament_id)
            if tier:
                logger.debug(f"tournament {tournament_id} resolved by another worker")
                return tier

            attempt += 1
            wait = min(RETRY_BACKOFF ** attempt, 60)
            wait += random.uniform(0, wait * 0.3)
            logger.debug(f"tournament {tournament_id}: fetch attempt {attempt} failed, retry in {wait:.1f}s")
            time.sleep(wait)

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

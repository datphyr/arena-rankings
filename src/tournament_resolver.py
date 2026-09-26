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
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

from config import BASE_URL, RETRY_BACKOFF
from src.fetcher import PageFetcher

logger = logging.getLogger(__name__)

# Default when tier can't be resolved.
DEFAULT_TIER = ""

# PlusForward's tournament listing (/tournaments/) encodes each event's tier as
# a CSS class on the row link: scale1=Premier, scale2=Major, scale3=Minor. The
# tournament page itself stopped carrying the tier keyword when the template was
# rewritten (2026-09), so this listing is now the only tier source. Results are
# cached by exact event name for the process lifetime — the parse stage builds a
# fresh resolver per post, hence the class-level cache.
_TIER_SCALE_MAP = {"scale1": "premier", "scale2": "major", "scale3": "minor"}


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
    # Exact event name -> tier, resolved from the /tournaments/ listing. Class
    # level so it survives across the per-post resolver instances the parse
    # stage creates.
    _tier_by_name_cache: dict[str, str] = {}

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
                    # Fast path: tier + html already stored. But if the name or
                    # game is empty (e.g. parsed before name/game extraction
                    # existed, or parsed by an older resolver that didn't know
                    # the current template), re-parse the full metadata from the
                    # cached HTML rather than leaving it blank. Reusing the
                    # normal resolve path keeps name/game/tier/schedule/prize/
                    # maplist/rankings consistent in one place.
                    name_rows = self._db.client.execute(
                        "SELECT name, game FROM tournaments FINAL WHERE tournament_id = %(t)s",
                        {"t": tournament_id},
                    )
                    existing_name = name_rows[0][0] if name_rows else ""
                    existing_game = name_rows[0][1] if name_rows else ""
                    if not existing_name or not existing_game:
                        TournamentResolver.db_hits += 1
                        return self._resolve_from_html(html, tournament_id)
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
        details = self._parse_tournament_details(html)
        # Prefer the name parsed from THIS page; fall back to the existing DB
        # name only if the page has no name (avoids clobbering a known name
        # with empty when resolving a tournament page without a title).
        name = self._parse_name(html) or self._get_existing_name(tournament_id)
        # Tier. The old template carried the keyword in the inner title; the new
        # template (2026-09) dropped it from the page entirely — only the
        # /tournaments/ listing still encodes it (scale1/2/3). So: page, then
        # listing by exact name, then keep the stored tier, and only guess
        # 'minor' as a last resort (never clobbering a real stored tier with it).
        tier = self._parse_tier(html, tournament_id)
        if not tier:
            tier = self._lookup_tier_via_listing(name)
        if not tier:
            tier = self._db.get_tournament_tier(tournament_id)
        if not tier:
            tier = "minor"
        # Game: prefer the page text; else resolve the pfcat category id via the
        # games table; else keep the stored game.
        game = details["game"]
        if not game:
            cat = details.get("game_category_id") or 0
            if cat:
                id_to_name = {gid: nm for nm, gid in self._db._game_id_cache().items()}
                game = id_to_name.get(cat, "")
        if not game:
            game = self._get_existing_game(tournament_id)
        # Resolve each rankings entry's game from its pfcat category id (the
        # canonical game name via the games table). The title-derived `game` is
        # only a fallback for entries whose category is unknown (0). Multi-game
        # event pages mix games; the row-level `game` only reflects the first.
        rankings_json = details["rankings"]
        if rankings_json not in ("", "[]"):
            try:
                import json as _json
                entries = _json.loads(rankings_json)
                id_to_name = {gid: nm for nm, gid in self._db._game_id_cache().items()}
                for e in entries:
                    if isinstance(e, dict):
                        cat = e.get("game_category_id") or 0
                        if cat:
                            e["game"] = id_to_name.get(cat, e.get("game", ""))
                rankings_json = _json.dumps(entries, ensure_ascii=False)
            except Exception:
                pass
        # Store the raw page HTML in raw_posts (post_id = tournament_id).
        self._db.store_raw_post(tournament_id, html, "parsed")
        self._db.upsert_tournament(
            tournament_id, name, tier,
            game=game, prize_money=details["prize_money"],
            tourney_format=details["tourney_format"], match_format=details["match_format"],
            schedule_start=details["schedule_start"], schedule_end=details["schedule_end"],
            maplist=details["maplist"], rankings=rankings_json,
        )
        if rankings_json != "[]" or details["maplist"]:
            TournamentResolver.parsed_details += 1
        return tier

    @staticmethod
    def _parse_name(html: str) -> str:
        """Extract the tournament name from the page title.

        Old template: <h1 class="posttitle"><a href="/post/123/...">Name</a></h1>
        New template (2026-09): <a class="title" href="/post/123/...">Name</a>
        inside the .overview block (no h1). The /post/<id>/ href pins it to
        this page's own post, so a comment/related link can't be mistaken for
        the title.
        """
        m = re.search(
            r'<h1 class="posttitle">\s*<a[^>]*href="/post/\d+/[^"]*">([^<]+)</a>',
            html, re.DOTALL)
        if not m:
            m = re.search(
                r'<a class="title" href="/post/\d+/[^"]*">([^<]+)</a>',
                html, re.DOTALL)
        if m:
            return _html.unescape(re.sub(r'<[^>]+>', '', m.group(1)).strip())
        return ""

    def _lookup_tier_via_listing(self, name: str) -> str:
        """Resolve a tournament tier from the /tournaments/ listing by name.

        The listing row carries the tier as a CSS class (scale1/2/3). Returns
        the tier, or '' when no row title matches exactly. Cached by name.
        """
        if not name:
            return ""
        cached = TournamentResolver._tier_by_name_cache.get(name)
        if cached is not None:
            return cached
        tier = ""
        try:
            url = f"{BASE_URL}/tournaments/?search={quote(name, safe='')}"
            html = self._fetcher.fetch(url, max_retries=1)
            if html:
                target = _html.unescape(name).strip()
                for row_name, cls in re.findall(
                        r'<a href="/post/\d+/[^"]*" title="([^"]*)" class="(scale\d)"',
                        html):
                    if _html.unescape(row_name).strip() == target:
                        tier = _TIER_SCALE_MAP.get(cls, "")
                        break
        except Exception as e:
            logger.debug(f"tier listing lookup failed for {name!r}: {e}")
        TournamentResolver._tier_by_name_cache[name] = tier
        return tier

    def _get_existing_name(self, tournament_id: int) -> str:
        rows = self._db.client.execute(
            "SELECT name FROM tournaments FINAL WHERE tournament_id = %(t)s",
            {"t": tournament_id},
        )
        return rows[0][0] if rows else ""

    def _get_existing_game(self, tournament_id: int) -> str:
        rows = self._db.client.execute(
            "SELECT game FROM tournaments FINAL WHERE tournament_id = %(t)s",
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

        New template (2026-09): returns '' — the page no longer carries the tier
        keyword at all; the caller resolves it from the /tournaments/ listing.
        """

        # New template: tier keyword was dropped from the page. Signal
        # "unknown" so the caller falls back to the listing/stored tier
        # instead of guessing minor and clobbering a real tier.
        if 'pf-tournament-head' in html:
            return ""

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

        # New template (2026-09) moved the headline + a key/value summary ABOVE
        # postinnercontent, into the .overview block inside .pf-tournament-head.
        # Slice it out so the new-template branches below can read it.
        overview = ""
        ov_m = re.search(r'<div class="overview">(.*?)<div id="postinnercontent"', html, re.DOTALL)
        if ov_m:
            overview = ov_m.group(1)

        def overview_values(label: str) -> list:
            """All values under a .overview summary label (e.g. Game, Date)."""
            om = re.search(
                r'<div class="label">' + re.escape(label) + r'</div>'
                r'((?:\s*<div class="value">.*?</div>)+)',
                overview, re.DOTALL)
            if not om:
                return []
            return [_html.unescape(re.sub(r'<[^>]+>', '', v)).strip()
                    for v in re.findall(r'<div class="value">(.*?)</div>', om.group(1), re.DOTALL)]

        # Game — from the inner title: <i class="pfcat-N"></i> [Tier] Game - Format
        game_m = re.search(
            r'<div class="title">\s*<i class="pfcat-\d+"></i>\s*(?:premier|major|minor\s+)?'
            r'([A-Za-z0-9 &\.\-]+?)(?:\s*[-–]\s*|\s*$)', body, re.DOTALL | re.IGNORECASE)
        if game_m:
            details["game"] = game_m.group(1).strip()
        # New template: game is the first .overview Game value.
        if not details["game"]:
            gv = overview_values("Game")
            if gv:
                details["game"] = gv[0]

        # Fallback game source: some (older) tournament pages have an empty
        # title after the icon, but still carry the pfcat category icon that
        # identifies the game. Stash the raw category id so the caller can
        # resolve it to a game name via the games table if the text parse
        # came up empty.
        details["game_category_id"] = 0
        cat_m = re.search(r'<div class="title">\s*<i class="pfcat-(\d+)"', body, re.DOTALL)
        if cat_m:
            details["game_category_id"] = int(cat_m.group(1))
        # New template: the page's own category icon lives in the .posthead
        # "Tournament" chip (<a class="cat"><i class="pfcat pfcat-N">).
        if not details["game_category_id"]:
            cat_m2 = re.search(
                r'<a class="cat"[^>]*>.*?<i class="pfcat pfcat-(\d+)"', html, re.DOTALL)
            if cat_m2:
                details["game_category_id"] = int(cat_m2.group(1))

        # Info block fields: <div class="tc_title">Label</div><div>Value</div>
        def info_value(label):
            pat = re.compile(
                r'<div class="tc_title">' + re.escape(label) + r'</div>\s*<div[^>]*>(.*?)</div>',
                re.DOTALL | re.IGNORECASE)
            m = pat.search(body)
            if not m:
                return ""
            val = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            # The source page HTML-escapes entities (e.g. "final &amp; Grand").
            # Unescape so we store the raw text; Jinja re-escapes once on output.
            # Without this, values like "&amp;" get double-escaped ("&amp;amp;").
            return _html.unescape(val)

        def info_title_value(label):
            """Value for a .tour_info field (new template uses info_title, not tc_title)."""
            pat = re.compile(
                r'<div class="info_title">' + re.escape(label) + r'</div>\s*<div[^>]*>(.*?)</div>',
                re.DOTALL | re.IGNORECASE)
            m = pat.search(body)
            if not m:
                return ""
            return _html.unescape(re.sub(r'<[^>]+>', '', m.group(1)).strip())

        details["prize_money"] = info_value("Prizemoney")
        # New template: prize is the .overview value under "Prize pool" (or,
        # on some pages, the shorter label "Prize").
        if not details["prize_money"]:
            pv = overview_values("Prize pool") or overview_values("Prize")
            if pv:
                details["prize_money"] = pv[0]
        details["tourney_format"] = info_value("Tourney format") or info_title_value("Tourney format")
        details["match_format"] = info_value("Match format") or info_title_value("Match format")

        # Schedule:
        #   Newer pages: <div class="tc_title">Schedule</div><div>
        #     <span title="29 Aug 2026 - 11:00 UTC">29 Aug 2026</span>
        #     <i ...arrow...></i>
        #     <span title="30 Aug 2026 - 20:15 UTC">30 Aug 2026</span></div>
        #   (the spans carry full UTC times in title=; the visible text is
        #   date-only — parse the title attrs when present)
        #   Older pages: ...<div>09 Aug 2026 - 16:00 UTC → 20:15 UTC</div>
        sched_m = re.search(
            r'<div class="tc_title">Schedule</div>\s*<div[^>]*>(.*?)</div>',
            body, re.DOTALL | re.IGNORECASE)
        if sched_m:
            sched_html = sched_m.group(1)
            # Preferred: exact UTC times from the span title attributes.
            span_ms = re.findall(
                r'<span title="(\d{1,2}\s+\w+\s+\d{4})\s*-\s*(\d{1,2}:\d{2})\s*UTC"',
                sched_html)
            try:
                if len(span_ms) >= 2:
                    start = datetime.strptime(span_ms[0][0], "%d %b %Y")
                    details["schedule_start"] = (
                        start.strftime("%Y-%m-%d ") + span_ms[0][1] + ":00")
                    end = datetime.strptime(span_ms[1][0], "%d %b %Y")
                    details["schedule_end"] = (
                        end.strftime("%Y-%m-%d ") + span_ms[1][1] + ":00")
                else:
                    # No title attributes: plain text date-only range
                    # ("29 Aug 2026 - 30 Aug 2026"). A date-only end means the
                    # event runs through that whole day — use 23:59 UTC so
                    # in-progress detection doesn't flip Finished at midnight
                    # UTC on the final day.
                    date_only_m = re.search(
                        r'(\d{1,2}\s+\w+\s+\d{4})\s*[-–>→]\s*(\d{1,2}\s+\w+\s+\d{4})',
                        re.sub(r'<[^>]+>', ' ', sched_html))
                    if date_only_m:
                        start = datetime.strptime(date_only_m.group(1), "%d %b %Y")
                        end_d = datetime.strptime(date_only_m.group(2), "%d %b %Y")
                        details["schedule_start"] = start.strftime("%Y-%m-%d 00:00:00")
                        details["schedule_end"] = end_d.strftime("%Y-%m-%d 23:59:00")
                    else:
                        # Legacy format with explicit times on the start day.
                        legacy_m = re.search(
                            r'(\d{1,2}\s+\w+\s+\d{4})\s*[-–]\s*(\d{1,2}:\d{2})\s*UTC'
                            r'(?:\s*<i[^>]*></i>\s*|\s*[-–>→]\s*|\s*)(\d{1,2}:\d{2})?\s*UTC?',
                            sched_html, re.DOTALL | re.IGNORECASE)
                        if legacy_m:
                            start = datetime.strptime(legacy_m.group(1), "%d %b %Y")
                            day = start.strftime("%Y-%m-%d ")
                            details["schedule_start"] = day + legacy_m.group(2) + ":00"
                            end_time = legacy_m.group(3) or legacy_m.group(2)
                            # End time can cross midnight (e.g. "22:00 UTC -> 03:00 UTC"
                            # next day). The schedule only lists hours on the start day, so
                            # if the end time is earlier than the start time it belongs to
                            # the following day — bump the date by one. Without this the end
                            # is stored before the start, making the event look like it
                            # ended before it began.
                            if int(end_time.split(":")[0]) < int(legacy_m.group(2).split(":")[0]):
                                end_day = (start + timedelta(days=1)).strftime("%Y-%m-%d ")
                            else:
                                end_day = day
                            details["schedule_end"] = end_day + end_time + ":00"
            except ValueError:
                pass

        # New template: the schedule lives in the .overview "Date" values, e.g.
        #   single-day: ["25 Sep 2026", "16:00 - 20:15 UTC"]
        #   multi-day:  ["07 Nov 2026 -", "08 Nov 2026"]
        if not details["schedule_start"]:
            date_vals = overview_values("Date")
            if date_vals:
                joined = " ".join(date_vals)
                dates = re.findall(r'(\d{1,2}\s+\w{3}\s+\d{4})', joined)
                times = re.search(
                    r'(\d{1,2}:\d{2})\s*[-–>→]\s*(\d{1,2}:\d{2})', joined)
                try:
                    if len(dates) >= 2:
                        # Multi-day: whole-day bounds (23:59 end so in-progress
                        # detection doesn't flip mid final day).
                        s = datetime.strptime(dates[0], "%d %b %Y")
                        e = datetime.strptime(dates[1], "%d %b %Y")
                        details["schedule_start"] = s.strftime("%Y-%m-%d 00:00:00")
                        details["schedule_end"] = e.strftime("%Y-%m-%d 23:59:00")
                    elif len(dates) == 1 and times:
                        s = datetime.strptime(dates[0], "%d %b %Y")
                        day = s.strftime("%Y-%m-%d ")
                        t1, t2 = times.group(1), times.group(2)
                        # End hour earlier than start hour = crossed midnight.
                        if int(t2.split(":")[0]) < int(t1.split(":")[0]):
                            end_day = (s + timedelta(days=1)).strftime("%Y-%m-%d ")
                        else:
                            end_day = day
                        details["schedule_start"] = day + t1 + ":00"
                        details["schedule_end"] = end_day + t2 + ":00"
                    elif len(dates) == 1:
                        s = datetime.strptime(dates[0], "%d %b %Y")
                        details["schedule_start"] = s.strftime("%Y-%m-%d 00:00:00")
                        details["schedule_end"] = s.strftime("%Y-%m-%d 23:59:00")
                except ValueError:
                    pass

        # Maplist: old template <span class="map_preview" data-name="Awoken">,
        # new template <div class="map_name">Awoken</div>.
        maplist = re.findall(
            r'<span class="map_preview"[^>]*data-name="([^"]+)"', body)
        if not maplist:
            maplist = re.findall(r'<div class="map_name">([^<]+)</div>', body)
        if maplist:
            details["maplist"] = [_html.unescape(x) for x in maplist]

        # Final rankings: .tour_rankings table rows.
        #   <td class="position">1st</td> ... <a class="profile" href="/player/16947/...">Name</a> ... <td class="prizemoney">60 USD</td>
        #
        # A single PlusForward event page can host MULTIPLE sub-tournaments, one
        # per game (e.g. ESWC, QuakeCon, TimCon pages list Q3 + QC + QL + ...
        # events with separate .tour_rankings tables). Parse each rankings block
        # in its own sub-event scope so every entry is tagged with the game it
        # actually belongs to (from that sub-event's pfcat category icon). This
        # lets downstream consumers (e.g. the player-page placements chart)
        # attribute placements to the correct game instead of the page's first.
        ranking_re = re.compile(
            r'<tr>.*?<td class="position">([^<]+)</td>.*?'
            r'<a class="profile" href="/player/(\d+)/[^"]*"[^>]*>(?:<span[^>]*></span>\s*)?([^<]+)</a>.*?'
            r'<td class="prizemoney">([^<]*)</td>',
            re.DOTALL)
        rankings = []
        # Locate each sub-event title and its pfcat category id, in page order.
        sub_titles = list(re.finditer(
            r'<div class="title">\s*<i class="pfcat-(\d+)"', body, re.DOTALL))
        for i, st in enumerate(sub_titles):
            cat = int(st.group(1))
            # This sub-event's block ends at the next sub-event title.
            end = sub_titles[i + 1].start() if i + 1 < len(sub_titles) else len(body)
            block = body[st.start():end]
            # Try to recover the game name from the title text for this block.
            title_game = ""
            gm = re.search(
                r'<i class="pfcat-\d+"></i>\s*<a href="[^"]*">([^<]+)</a>',
                block, re.DOTALL)
            if gm:
                title_game = re.sub(r"^[^-]+-\s*", "", _html.unescape(gm.group(1)).strip()).strip()
            for pos, pid, pname, prize in ranking_re.findall(block):
                rankings.append({
                    "position": pos.strip(),
                    "player_id": int(pid),
                    "player_name": pname.strip(),
                    "prize": prize.strip(),
                    "game": title_game,
                    "game_category_id": cat,
                })
        # Fallback: if no titled sub-events were found, parse the whole body as
        # one block (older/simple pages with a single rankings table).
        if not rankings:
            for pos, pid, pname, prize in ranking_re.findall(body):
                rankings.append({
                    "position": pos.strip(),
                    "player_id": int(pid),
                    "player_name": pname.strip(),
                    "prize": prize.strip(),
                    "game": "",
                    "game_category_id": 0,
                })
        # New template: .tour_rankings holds <div class="row"> blocks, one per
        # placement, each with .position, a .entity profile link (carrying the
        # pfcat category id in the href) and .prizemoney.
        if not rankings:
            idx = body.find('tour_rankings')
            if idx >= 0:
                for chunk in body[idx:].split('<div class="row">')[1:]:
                    pm = re.search(r'<div class="position">([^<]+)</div>', chunk)
                    if not pm:
                        continue
                    # Prefer the 4-segment href (player/game/cat) so the entry
                    # keeps its game category; fall back to the 2-segment form.
                    em = re.search(
                        r'<a class="profile" href="/player/(\d+)/(?:[^/]+)/(\d+)/[^"]*"[^>]*>(.*?)</a>',
                        chunk, re.DOTALL)
                    if em:
                        pid, cat, raw_name = int(em.group(1)), int(em.group(2)), em.group(3)
                    else:
                        em = re.search(
                            r'<a class="profile" href="/player/(\d+)/[^"]*"[^>]*>(.*?)</a>',
                            chunk, re.DOTALL)
                        if not em:
                            continue
                        pid, cat, raw_name = int(em.group(1)), 0, em.group(2)
                    pname = _html.unescape(re.sub(r'<[^>]+>', '', raw_name)).strip()
                    # Placeholder rows (no real player name) are skipped so a
                    # pending standings table never fabricates a placement.
                    if not pname:
                        continue
                    zm = re.search(r'<div class="prizemoney">([^<]*)</div>', chunk)
                    rankings.append({
                        "position": pm.group(1).strip(),
                        "player_id": pid,
                        "player_name": pname,
                        "prize": zm.group(1).strip() if zm else "",
                        "game": "",
                        "game_category_id": cat,
                    })
        if rankings:
            details["rankings"] = json.dumps(rankings, ensure_ascii=False)

        return details

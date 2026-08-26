#!/usr/bin/env python3
"""One-off: refresh a tournament that was left with incomplete final standings.

Tournament 94897 (EGB QC Cup #6) got its last match (Grand Final) parsed while
PlusForward still showed placeholder links (/player/20/... empty name) for the
1st/2nd slots, and the EGB bracket still showed the Grand Final unscored. The
refresh is event-driven (per new match), so after the Grand Final nothing
re-fetched the page and the stale standings/bracket stuck.

This script re-downloads the tournament page, re-parses it through the normal
pipeline path (_parse_post), and force-refetches the bracket.

Usage:  python3 fix_tournament.py <tournament_id> [more ids...]
"""
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.db_client import Database
from src.fetcher import PageFetcher
from src.match_parser import MatchDetailParser, _parse_post, _is_tournament_post
from src.tournament_resolver import TournamentResolver
from src.bracket_fetcher import BracketFetcher

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fix_tournament")


def refresh(tournament_id: int) -> bool:
    db = Database()
    try:
        fetcher = PageFetcher()
        parser = MatchDetailParser()
        resolver = TournamentResolver(db, fetcher)
        bracket_fetcher = BracketFetcher(db, fetcher)

        # 1. Re-download the live tournament page.
        url = f"https://plusforward.net/post/{tournament_id}/"
        logger.info(f"downloading {url}")
        html = fetcher.fetch(url)
        if not html:
            logger.error(f"no html for {tournament_id}")
            return False

        # 2. Verify it's still a tournament post, then force re-parse it.
        #    _parse_post -> resolver.resolve() hits the tier+html fast-path and
        #    skips re-computing rankings, so call resolve(force=True) to
        #    re-parse standings from the freshly cached page.
        if not _is_tournament_post(html):
            logger.error(f"post {tournament_id} no longer parses as a tournament post")
            return False
        db.store_raw_post(tournament_id, html, status="downloaded")
        tier = resolver.resolve(tournament_id, force=True)
        logger.info(f"resolve(force=True) -> tier={tier!r}")

        # 3. Force-refresh the bracket (EGB API is now complete).
        try:
            bracket_fetcher.fetch_for_tournament_if_needed(tournament_id, force=True)
        except Exception as e:
            logger.warning(f"bracket force-refresh failed: {e}")

        # 4. Report the resulting standings.
        det = db.get_tournament_details(tournament_id)
        if det:
            import json
            rankings = json.loads(det["rankings"] or "[]")
            logger.info(f"standings for {tournament_id}:")
            for r in rankings:
                logger.info("  %s: %s (id=%s) %s", r.get("position"),
                            r.get("player_name"), r.get("player_id"), r.get("prize", ""))
        br = db.get_tournament_bracket(tournament_id)
        if br and br.get("data"):
            logger.info("bracket complete=%s source=%s fetched_at=%s",
                        br["data"].get("complete"), br.get("source"), br.get("fetched_at"))
        return True
    finally:
        db.close()


if __name__ == "__main__":
    ids = [int(a) for a in sys.argv[1:]] or [94897]
    for tid in ids:
        print(f"--- refreshing tournament {tid} ---")
        refresh(tid)

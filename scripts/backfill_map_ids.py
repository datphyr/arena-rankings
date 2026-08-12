#!/usr/bin/env python3
"""Backfill map IDs into match_maps and populate the maps lookup table.

One-time migration to make map_id the primary key for maps (instead of names).

What it does:
  1. ALTER match_maps to add the map_id column (idempotent — skips if present).
  2. Create the maps lookup table if missing (map_id -> name, slug, image, game).
  3. Populate the maps table from match_maps (maps that were actually played),
     with tournament data as a fallback for map_ids absent from match_maps
     (maps listed in tournament pages but never parsed into match_maps).
     match_maps takes priority for name/game; tournament fills the gaps.
     Resolves the canonical name, slug, image path, and the single game.
  4. Re-parse cached match raw_html (offline, no network) and re-insert
     match_maps rows with map_id filled. ReplacingMergeTree dedups on
     (played_at, match_id, map_index), so re-inserting is safe/idempotent.

Usage:
    python3 -m scripts.backfill_map_ids               # full backfill
    python3 -m scripts.backfill_map_ids --limit 200   # test run (maps only)
    python3 -m scripts.backfill_map_ids --skip-maps   # only fill match_maps.map_id
    python3 -m scripts.backfill_map_ids --skip-reparse # only build the maps table
"""

import argparse
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db_client import Database
from src.match_parser import MatchDetailParser

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill-map-ids")


def _slug(name: str) -> str:
    """URL-safe readability slug (lowercase, spaces->dashes)."""
    s = re.sub(r"[^\w\- ]", "", name or "").strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    return s or "map"


def ensure_schema(db: Database):
    """Add map_id to match_maps and create the maps table if missing."""
    cols = {r[0] for r in db.client.execute("DESCRIBE match_maps")}
    if "map_id" not in cols:
        logger.info("ALTER match_maps ADD COLUMN map_id")
        db.client.execute(
            "ALTER TABLE match_maps ADD COLUMN IF NOT EXISTS "
            "map_id UInt32 DEFAULT 0 AFTER map_index"
        )
    else:
        logger.info("match_maps.map_id already present")

    tables = {r[0] for r in db.client.execute("SHOW TABLES")}
    if "maps" not in tables:
        logger.info("CREATE TABLE maps")
        db.client.execute(
            """
            CREATE TABLE IF NOT EXISTS maps (
                map_id UInt32,
                name String DEFAULT '',
                slug String DEFAULT '',
                image String DEFAULT '',
                game String DEFAULT ''
            )
            ENGINE = ReplacingMergeTree()
            ORDER BY map_id
            """
        )
    else:
        logger.info("maps table already present")


def build_maps_table(db: Database, limit: int = 0):
    """Populate the maps lookup table from match_maps, with tournament fallback.

    The maps table reflects maps that were actually played (match_maps rows),
    PLUS maps referenced only in tournament pages (never parsed into
    match_maps). match_maps takes priority: for a map_id present in both, the
    name and game come from match_maps; tournament data is used only as a
    fallback for map_ids absent from match_maps. Games are the distinct
    game_name values seen for each map_id across its match_maps rows (or the
    tournament's game for tournament-only maps).
    """
    # map_id -> {name, game, count}
    id_info: dict[int, dict] = {}

    # Recover PlusForward's real image slug per map_id from match raw_html
    # (the exact data-image URL). Falls back to a name-derived slug below when
    # a map_id has no raw_html image.
    real_slug: dict[int, str] = {}
    raw_rows = db.client.execute(
        "SELECT raw_html FROM match_registry WHERE raw_html LIKE '%data-image=%'"
    )
    for (raw,) in raw_rows:
        if not raw:
            continue
        for m in re.finditer(r"/files/images/maps/(\d+)_([^/]+)\.jpg", raw):
            real_slug.setdefault(int(m.group(1)), m.group(2))

    # Pull every played map from match_maps, joined to matches for the game.
    rows = db.client.execute(
        """
        SELECT mm.map_id, mm.map_name, m.game_name
        FROM match_maps mm
        LEFT JOIN matches m ON m.match_id = mm.match_id
        WHERE mm.map_id > 0
        """
    )
    if limit:
        rows = rows[:limit]

    for mid, name, game in rows:
        name = (name or "").strip() or "?"
        info = id_info.setdefault(mid, {"name": name, "game": "", "count": 0})
        info["name"] = name
        info["count"] += 1
        if game and not info["game"]:
            info["game"] = game

    logger.info(f"collected {len(id_info)} distinct map IDs from match_maps")

    # --- Tournament fallback: map_ids absent from match_maps ---
    # For each tournament, its map stats (data-image + data-name) reference
    # maps; the tournament's own game column supplies the game. Only add map_ids
    # not already seen in match_maps (match_maps takes priority).
    t_rows = db.client.execute(
        "SELECT game, raw_html FROM tournaments WHERE raw_html LIKE '%data-image=%'"
    )
    t_added = 0
    for game, raw in t_rows:
        if not raw:
            continue
        for m in re.finditer(
            r'data-image="[^"]*/maps/(\d+)_[^"]*"[^>]*data-name="([^"]*)"', raw
        ):
            mid = int(m.group(1))
            if mid in id_info:
                continue  # match_maps wins
            name = (m.group(2) or "").strip() or "?"
            info = id_info.setdefault(mid, {"name": name, "game": "", "count": 0})
            info["name"] = name
            if game and not info["game"]:
                info["game"] = game
            t_added += 1
    if t_added:
        logger.info(f"added {t_added} map IDs from tournament fallback")

    # Build rows for the maps table. Image URL uses PlusForward's real slug
    # (recovered from match raw_html) when available, else the name-derived slug.
    rows_out = []
    for mid, info in id_info.items():
        slug = real_slug.get(mid) or _slug(info["name"])
        rows_out.append((
            mid,
            info["name"],
            slug,
            f"/files/images/maps/{mid}_{slug}.jpg",
            info["game"],
        ))

    # Insert (ReplacingMergeTree dedups on map_id).
    db.client.execute(
        "INSERT INTO maps (map_id, name, slug, image, game) VALUES",
        rows_out,
    )
    logger.info(f"inserted {len(rows_out)} rows into maps table")
    return len(rows_out)


def reparse_match_maps(db: Database, workers: int = 8, limit: int = 0):
    """Re-parse cached match raw_html and re-insert match_maps with map_id."""
    rows = db.client.execute(
        "SELECT match_id, played_at, raw_html FROM match_registry FINAL "
        "WHERE raw_html != '' AND status = 'parsed' "
        "ORDER BY played_at DESC"
    )
    if limit:
        rows = rows[:limit]
    total = len(rows)
    if total == 0:
        logger.info("no matches to re-parse")
        return 0

    logger.info(f"{total} matches to re-parse | {workers}w")
    out_db = Database()
    done = 0
    maps_fixed = 0
    start = time.time()

    def _worker(task):
        match_id, played_at, raw_html = task
        parser = MatchDetailParser()
        detail = parser.parse(raw_html, match_id)
        return match_id, detail

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_worker, t): t[0] for t in rows}
        for fut in as_completed(futures):
            mid = futures[fut]
            try:
                _, detail = fut.result()
            except Exception as e:
                logger.warning(f"reparse error {mid}: {e}")
                done += 1
                continue
            if detail is not None and detail.maps:
                out_db.insert_match_maps(detail.match_id, detail.maps, detail.played_at)
                maps_fixed += 1
            done += 1
            if done % 1000 == 0 or done == total:
                elapsed = time.time() - start
                rate = done / elapsed if elapsed > 0 else 0
                logger.info(f"{done}/{total} — {maps_fixed} matches with maps, {rate:.0f}/s")

    out_db.close()
    logger.info(f"done: {done}/{total} in {time.time()-start:.0f}s — {maps_fixed} matches have map rows")
    return maps_fixed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process N matches/rows (test run)")
    parser.add_argument("--skip-maps", action="store_true",
                        help="Skip building the maps table (only fill match_maps.map_id)")
    parser.add_argument("--skip-reparse", action="store_true",
                        help="Skip re-parsing match_maps (only build the maps table)")
    parser.add_argument("--workers", type=int, default=8, help="Parser threads")
    args = parser.parse_args()

    db = Database()
    ensure_schema(db)

    if not args.skip_maps:
        build_maps_table(db, limit=args.limit)

    if not args.skip_reparse:
        reparse_match_maps(db, workers=args.workers, limit=args.limit)

    db.close()
    logger.info("backfill complete")


if __name__ == "__main__":
    main()

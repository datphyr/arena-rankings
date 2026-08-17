#!/usr/bin/env python3
"""Backfill per-match player country columns (player1_country/player2_country)
in the matches table from the raw HTML of already-parsed match posts.

The parser extracts each player's country flag (s_flag-XX) from the match page
but previously only the last-written value survived in players.country. This
re-parses the stored raw HTML of existing parsed matches and writes each match's
two player country codes, so we can aggregate "most common flag per player".

Usage:
    python3 backfill_match_countries.py            # full backfill
    python3 backfill_match_countries.py --limit 500  # first 500 (dry sizing)
    python3 backfill_match_countries.py --workers 8
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.db_client import Database
from src.match_parser import MatchDetailParser


def _process(task: tuple) -> tuple[int, bool, str]:
    """Parse one match's HTML and update its country columns.

    Returns (match_id, success, error).
    """
    match_id, p1_id, p2_id, raw_html = task
    try:
        parser = MatchDetailParser()
        detail, reason = parser.parse_with_reason(raw_html, match_id)
        if detail is None:
            return match_id, False, reason or "parse error"
        return match_id, True, (detail.player1_country, detail.player2_country)
    except Exception as e:
        return match_id, False, str(e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max matches (0 = all)")
    ap.add_argument("--workers", type=int, default=4, help="parser threads")
    args = ap.parse_args()

    db = Database()
    rows = db.client.execute(
        """
        SELECT m.match_id, m.player1_id, m.player2_id, r.raw_html
        FROM matches m FINAL
        JOIN raw_posts r FINAL ON r.post_id = m.match_id
        WHERE r.status = 'parsed' AND r.raw_html != ''
        """
    )
    db.close()
    total = len(rows)
    print(f"{total} parsed matches to backfill")
    if args.limit:
        rows = rows[:args.limit]
        total = len(rows)
        print(f"limited to {total}")

    if total == 0:
        print("nothing to do")
        return 0

    ok = 0
    fail = 0
    errors = {}
    start = time.time()
    done = 0

    def _flush(results):
        nonlocal ok, fail
        # Batch-update countries. matches is ReplacingMergeTree; country codes are
        # simple [a-z]{2,3} strings (or ''), so building safe literal SQL is fine.
        updates = []
        for match_id, success, payload in results:
            if success:
                updates.append((payload[0], payload[1], match_id))
                ok += 1
            else:
                fail += 1
                errors[payload or "unknown"] = errors.get(payload or "unknown", 0) + 1
        if not updates:
            return

        def _lit(s):
            s = (s or "").replace("'", "''")
            return f"'{s}'"

        db = Database()
        try:
            # Per-row UPDATEs (batch of ALTER UPDATE statements in one round-trip).
            stmts = [
                "ALTER TABLE matches UPDATE "
                f"player1_country = {_lit(c1)}, player2_country = {_lit(c2)} "
                f"WHERE match_id = {mid}"
                for c1, c2, mid in updates
            ]
            for stmt in stmts:
                db.client.execute(stmt)
        finally:
            db.close()

    workers = args.workers
    if workers <= 1:
        for i, task in enumerate(rows, 1):
            _flush([_process(task)])
            done = i
            if done % 200 == 0 or done == total:
                print(f"{done}/{total} ok={ok} fail={fail} {time.time()-start:.0f}s")
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_process, t) for t in rows]
            batch = []
            for fut in as_completed(futures):
                batch.append(fut.result())
                done += 1
                if len(batch) >= 200 or done == total:
                    _flush(batch)
                    batch = []
                    if done % 500 == 0 or done == total:
                        print(f"{done}/{total} ok={ok} fail={fail} {time.time()-start:.0f}s")

    print(f"\nDONE ok={ok} fail={fail} ({time.time()-start:.0f}s)")
    if errors:
        print("error breakdown:", dict(sorted(errors.items(), key=lambda kv: -kv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""One-off full sweep: AJAX-probe every tournament that still has no bracket.

Runs after backfill_brackets.py. Self-gating: fetch_for_tournament_if_needed
skips tournaments that already have a stored bracket, so this only probes the
remaining static-miss pages (all eras, not just 2024+).
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from src.db_client import Database
from src.bracket_fetcher import BracketFetcher


def main():
    db = Database()
    f = BracketFetcher(db)
    have = {r[0] for r in db.client.execute("SELECT DISTINCT tournament_id FROM tournament_brackets FINAL")}
    rows = db.client.execute(
        "SELECT post_id, raw_html FROM raw_posts FINAL "
        "WHERE post_id IN (SELECT tournament_id FROM tournaments FINAL) AND raw_html != ''")
    targets = [tid for tid, html in rows if tid not in have]
    print(f"full sweep: {len(targets)} bracketless tournaments to probe", flush=True)
    ok = 0
    for i, tid in enumerate(targets, 1):
        try:
            if f.fetch_for_tournament_if_needed(tid):
                ok += 1
        except Exception as e:
            print(f"  ! {tid}: {e}", flush=True)
        if i % 100 == 0:
            print(f"  {i}/{len(targets)} (stored {ok})", flush=True)
    print(f"full sweep done: {ok}/{len(targets)} stored", flush=True)
    n = db.client.execute("SELECT count() FROM tournament_brackets FINAL")[0][0]
    print(f"TOTAL brackets stored: {n}")
    db.close()


if __name__ == "__main__":
    main()
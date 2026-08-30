#!/usr/bin/env python3
"""One-off: re-fetch tournament brackets wiped by `reset.py parsed` (2026-08-29).

Phase 1: every tournament whose cached page statically links a bracket
         provider (toornament/shambler/egb/kuachi) -> force re-fetch.
Phase 2: recent (2024+) tournaments whose static detection misses ->
         AJAX probe (that's how plusforward-native brackets are found),
         which is the 93477-style group.
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

    static_ids = [int(x) for x in open("/tmp/static_bracket_ids.txt").read().split()]
    print(f"phase 1: {len(static_ids)} static-source tournaments")
    ok = 0
    for i, tid in enumerate(static_ids, 1):
        try:
            if f.fetch_for_tournament_if_needed(tid):
                ok += 1
        except Exception as e:
            print(f"  ! {tid}: {e}")
        if i % 25 == 0:
            print(f"  phase1 {i}/{len(static_ids)} (stored {ok})", flush=True)
    print(f"phase 1 done: {ok}/{len(static_ids)} brackets stored")

    # Phase 2: AJAX probe for recent tournaments with no static source.
    recent_ids = {r[0] for r in db.client.execute(
        "SELECT DISTINCT tournament_id FROM matches FINAL WHERE played_at >= '2024-01-01'")}
    phase2 = []
    for chunk_start in range(0, len(recent_ids), 500):
        ids = list(recent_ids)[chunk_start:chunk_start + 500]
        rows = db.client.execute(f"SELECT post_id, raw_html FROM raw_posts FINAL WHERE post_id IN ({','.join(map(str, ids))})")
        for tid, html in rows:
            if tid not in static_ids and html and not f.detect_source(html):
                phase2.append(tid)
    print(f"phase 2: {len(phase2)} ajax probes")
    ok2 = 0
    for i, tid in enumerate(phase2, 1):
        try:
            if f.fetch_for_tournament_if_needed(tid):
                ok2 += 1
        except Exception as e:
            print(f"  ! {tid}: {e}")
        if i % 25 == 0:
            print(f"  phase2 {i}/{len(phase2)} (stored {ok2})", flush=True)
    print(f"phase 2 done: {ok2}/{len(phase2)} native brackets stored")
    n = db.client.execute("SELECT count() FROM tournament_brackets FINAL")[0][0]
    print(f"TOTAL brackets stored: {n}")
    db.close()


if __name__ == "__main__":
    main()
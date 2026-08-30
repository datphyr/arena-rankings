"""Per-stage process entrypoint.

Usage:
    python -m engine.stage download [--workers 4] [--limit 20] [--idle 5]
    python -m engine.stage parse    [--workers 2] [--idle 5]
    python -m engine.stage rank     [--game X] [--system both] [--idle 60]
    python -m engine.stage discovery [--max-pages 5] [--idle 30]

Each maps a stage's `run_cycle` into the shared `engine.runner.run_stage` loop,
providing the `has_work` predicate so idle stages sleep cheaply instead of
polling blindly.
"""

from __future__ import annotations

import argparse
import logging
import sys

from config import PARSER_WORKERS
from engine import runner
from engine import stages

from src.logging_setup import configure_logging


def _has_download_work() -> bool:
    from engine.queue import PipelineQueue
    q = PipelineQueue(pending_status="discovered")
    try:
        return q.pending_count() > 0
    finally:
        q.db.close()


def _has_parse_work() -> bool:
    from engine.queue import PipelineQueue
    q = PipelineQueue(pending_status="downloaded")
    try:
        return q.pending_count() > 0
    finally:
        q.db.close()


def _has_rank_work() -> bool:
    from src.db_client import Database
    db = Database()
    try:
        rows = db.client.execute(
            "SELECT count() FROM matches FINAL WHERE match_id > 0"
        )
        return bool(rows and rows[0][0] > 0)
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a single pipeline stage process")
    parser.add_argument("stage", choices=["download", "parse", "rank", "discovery", "reconcile"])
    # Default parse/db workers to CPU-core count (PARSER_WORKERS env overrides),
    # matching the batch path's convention (parse_all_matches). Explicit flag
    # still wins.
    parser.add_argument("--workers", type=int, default=PARSER_WORKERS)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--game", default="")
    parser.add_argument("--system", default="both", choices=["elo", "glicko2", "both"])
    parser.add_argument("--idle", type=float, default=None,
                        help="Idle seconds between cycles when no work (per-stage default if omitted)")
    parser.add_argument("--max-backoff", type=float, default=300.0)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    configure_logging(verbose=args.verbose)

    if args.stage == "download":
        from engine.stages.download import run_cycle
        return runner.run_stage(
            "download",
            lambda: run_cycle(workers=args.workers, limit=args.limit),
            has_work=_has_download_work,
            idle_delay=args.idle if args.idle is not None else 5.0,
            max_backoff=args.max_backoff,
        )
    if args.stage == "parse":
        from engine.stages.parse import run_cycle
        return runner.run_stage(
            "parse",
            lambda: run_cycle(workers=args.workers, limit=args.limit),
            has_work=_has_parse_work,
            idle_delay=args.idle if args.idle is not None else 5.0,
            max_backoff=args.max_backoff,
        )
    if args.stage == "rank":
        from engine.stages.rank import run_cycle
        return runner.run_stage(
            "rank",
            lambda: run_cycle(game_filter=args.game, system=args.system),
            has_work=_has_rank_work,
            idle_delay=args.idle if args.idle is not None else 60.0,
            max_backoff=args.max_backoff,
        )
    if args.stage == "discovery":
        from engine.stages.discovery import run_cycle
        # Discovery is a poller; when caught up, wait a full cycle before
        # scanning again (was 60s in the old pipeline). The `empty` flag from
        # run_cycle makes the runner use this idle delay instead of hot-looping.
        return runner.run_stage(
            "discovery",
            lambda: run_cycle(max_pages=args.max_pages),
            idle_delay=args.idle if args.idle is not None else 60.0,
            max_backoff=args.max_backoff,
        )
    if args.stage == "reconcile":
        from engine.stages.reconcile import run_cycle
        # Reconciliation is a periodic sweep (interval-gated inside run_cycle).
        # No has_work predicate: it always runs its own cheap cycle. Short idle
        # so the interval is reached promptly; the gate keeps it cheap.
        return runner.run_stage(
            "reconcile",
            lambda: run_cycle(),
            idle_delay=args.idle if args.idle is not None else 30.0,
            max_backoff=args.max_backoff,
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())

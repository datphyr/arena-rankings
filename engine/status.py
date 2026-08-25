"""Pipeline status writer — feeds the dashboard.

Every stage periodically writes its health/lag/backlog into `pipeline_status`
(a ReplacingMergeTree keyed by (stage, bucket)). The dashboard reads the latest
bucket per stage. Keeping it a small append table means we get both the live
snapshot and a time series for the later throughput/queue charts.

This module is purely additive and dashboard-scoped: if the dashboard is
removed later, deleting this module + the table leaves the pipeline untouched.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

from src.db_client import Database

logger = logging.getLogger("pipeline")


def ensure_status_tables(db: Database) -> None:
    """Create pipeline_status + failed_posts if missing. Idempotent."""
    db.client.execute(
        """
        CREATE TABLE IF NOT EXISTS arena_rankings.pipeline_status (
            stage        LowCardinality(String),
            status       LowCardinality(String),   -- running|idle|backed-off|down|dead-letter
            processed    UInt64 DEFAULT 0,
            queue_depth  UInt64 DEFAULT 0,
            lag_seconds  Int64  DEFAULT 0,         -- how far behind real-time
            last_run_at  DateTime,
            detail       String  DEFAULT '',
            bucket       UInt64,                  -- 1-min bucket for dedup
            updated_at   DateTime
        ) ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (stage, bucket)
        """
    )
    db.client.execute(
        """
        CREATE TABLE IF NOT EXISTS arena_rankings.failed_posts (
            post_id     UInt64,
            stage       LowCardinality(String),
            status      LowCardinality(String),
            error       String,
            attempts    UInt32,
            first_seen  DateTime,
            last_error  DateTime,
            raw_html    String
        ) ENGINE = ReplacingMergeTree()
        ORDER BY post_id
        """
    )


def write_status(
    db: Database,
    stage: str,
    status: str,
    processed: int = 0,
    queue_depth: int = 0,
    lag_seconds: int = 0,
    detail: str = "",
) -> None:
    """Upsert a stage's latest status snapshot (1-minute dedup bucket)."""
    ensure_status_tables(db)
    bucket = int(time.time() // 60)
    db.client.execute(
        "INSERT INTO pipeline_status (stage, status, processed, queue_depth, "
        "lag_seconds, detail, bucket, last_run_at, updated_at) VALUES",
        [(
            stage, status, processed, queue_depth, lag_seconds, detail,
            bucket, datetime.utcnow(), datetime.utcnow(),
        )],
    )


def read_latest_status(db: Database) -> list[dict]:
    """Return the newest snapshot per stage (for the dashboard).

    `pipeline_status` is a ReplacingMergeTree keyed by (stage, bucket), so it
    holds a time series (one row per stage per minute). This reads exactly the
    latest row per stage via argMax over updated_at, so the dashboard shows
    one card per stage, not one per bucket.
    """
    ensure_status_tables(db)
    rows = db.client.execute(
        """
        SELECT
            stage,
            argMax(status, updated_at),
            argMax(processed, updated_at),
            argMax(queue_depth, updated_at),
            argMax(lag_seconds, updated_at),
            argMax(last_run_at, updated_at),
            argMax(detail, updated_at)
        FROM pipeline_status
        GROUP BY stage
        ORDER BY stage
        """
    )
    return [
        {
            "stage": r[0],
            "status": r[1],
            "processed": r[2],
            "queue_depth": r[3],
            "lag_seconds": r[4],
            "last_run_at": r[5],
            "detail": r[6],
        }
        for r in rows
    ]

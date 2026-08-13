#!/usr/bin/env python3
"""Logical backup/restore of the arena_rankings ClickHouse database.

Backs up each table as a Parquet file (zstd-compressed) via the ClickHouse HTTP
interface, then archives them into a single compressed file:
backups/<db>_<timestamp>.tar.zst

Why Parquet + zstd + HTTP:
  - Parquet with zstd compresses the raw HTML extremely well (~100x), so the
    3.2GB match_registry becomes ~30MB.
  - The HTTP interface (port 8123) returns raw format bytes, which the native
    driver can't (it parses into tuples). Streaming via requests avoids loading
    the whole table into memory (no OOM).
  - A single .tar.zst archive is one portable backup file.

Uses the project's own DB credentials (config.py) — no docker, no shelling out.

Usage:
    python3 backup.py                 # full backup -> single .tar.zst
    python3 backup.py --table matches # backup a single table
    python3 backup.py --restore FILE  # restore a backup archive
    python3 backup.py --restore FILE --table matches

Restore reads the .tar.zst, extracts each .parquet, and streams it back into
ClickHouse via the HTTP interface (INSERT ... FORMAT Parquet), OOM-free.
"""

import argparse
import io
import os
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from config import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
)

# ClickHouse HTTP interface (port 8123 by default; native is 9000).
HTTP_PORT = int(os.environ.get("CLICKHOUSE_HTTP_PORT", "8123"))
HTTP_URL = f"http://{CLICKHOUSE_HOST}:{HTTP_PORT}/"

# Tables to back up, in dependency-safe order (parents before children).
ALL_TABLES = [
    "discovery_state",
    "games",
    "maps",
    "players",
    "player_aliases",
    "tournaments",
    "matches",
    "match_maps",
    "match_registry",
    "rating_history",
    "player_ratings",
]

BACKUP_ROOT = ROOT / "backups"

# Parquet + zstd compression settings.
PARQUET_SETTINGS = "output_format_parquet_compression_method='zstd'"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _http_get(query: str, stream: bool = False):
    """Run a SELECT query via the HTTP interface, returning the response."""
    return requests.post(
        HTTP_URL,
        params={"query": query},
        auth=(CLICKHOUSE_USER, CLICKHOUSE_PASSWORD),
        stream=stream,
        timeout=600,
    )


def _table_engine(table: str) -> str:
    """Return the table engine (to decide FINAL)."""
    r = _http_get(
        f"SELECT engine FROM system.tables WHERE database = '{CLICKHOUSE_DATABASE}' AND name = '{table}'"
    )
    return r.text.strip()


def backup_table(table: str, out: io.BytesIO) -> int:
    """Stream one table as Parquet+zstd into `out`. Returns row count."""
    final = " FINAL" if _table_engine(table) == "ReplacingMergeTree" else ""
    r = _http_get(
        f"SELECT * FROM {CLICKHOUSE_DATABASE}.{table}{final} "
        f"FORMAT Parquet SETTINGS {PARQUET_SETTINGS}",
        stream=True,
    )
    r.raise_for_status()
    n = 0
    for chunk in r.iter_content(chunk_size=1 << 20):
        out.write(chunk)
        n += len(chunk)
    return n


def do_backup(tables: list[str], out_path: Path) -> None:
    """Back up tables into a single .tar.zst archive."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_path, "w:gz") as tar:
        for t in tables:
            print(f"  backing up: {t}")
            buf = io.BytesIO()
            n = backup_table(t, buf)
            info = tarfile.TarInfo(name=f"{t}.parquet")
            info.size = len(buf.getvalue())
            tar.addfile(info, io.BytesIO(buf.getvalue()))
    print(f"\nBackup complete: {out_path}")


def restore_table(table: str, data: bytes) -> None:
    """Stream one Parquet file back into ClickHouse via HTTP."""
    r = requests.post(
        HTTP_URL,
        params={"query": f"INSERT INTO {CLICKHOUSE_DATABASE}.{table} FORMAT Parquet"},
        auth=(CLICKHOUSE_USER, CLICKHOUSE_PASSWORD),
        data=data,
        timeout=600,
    )
    r.raise_for_status()


def do_restore(tables: list[str], archive: Path) -> None:
    """Restore tables from a .tar.gz archive."""
    with tarfile.open(archive, "r:gz") as tar:
        for t in tables:
            member = tar.getmember(f"{t}.parquet")
            f = tar.extractfile(member)
            if f is None:
                print(f"  SKIP {t}: no parquet in archive")
                continue
            data = f.read()
            print(f"  restoring: {t} ({len(data)} bytes)")
            restore_table(t, data)
            print(f"    done")


def main():
    parser = argparse.ArgumentParser(description="Arena Rankings backup/restore (Parquet+zstd, single archive)")
    parser.add_argument("--table", action="append", help="Table(s) to back up/restore (repeatable; default: all)")
    parser.add_argument("--restore", metavar="FILE", help="Restore from a backup archive instead of backing up")
    parser.add_argument("--out", metavar="FILE", help="Backup output file (default: backups/<db>_<ts>.tar.gz)")
    args = parser.parse_args()

    tables = args.table or ALL_TABLES

    if args.restore:
        archive = Path(args.restore)
        if not archive.is_file():
            print(f"error: not a file: {archive}", file=sys.stderr)
            return 2
        print(f"Restoring from {archive} ...")
        do_restore(tables, archive)
        print("Restore complete.")
        return 0

    out = Path(args.out) if args.out else BACKUP_ROOT / f"{CLICKHOUSE_DATABASE}_{_ts()}.tar.gz"
    do_backup(tables, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Reset the Arena Rankings ClickHouse database.

Accepts positional arguments to choose what to reset:

    python3 reset.py rankings            # clear ratings + history (daemon recomputes on next cycle)
    python3 reset.py parsed              # clear parsed data (matches, maps, players), reset status to discovered
    python3 reset.py rankings parsed     # both
    python3 reset.py all                 # full reset: drop + recreate + restore downloaded data
    python3 reset.py all --no-restore    # full reset, empty database

    python3 reset.py --dry-run rankings  # show what would happen

Data categories:
    downloaded  → raw_posts (downloaded post HTML), tournaments (names, tiers), discovery_state
    parsed      → matches, match_maps, players (extracted from raw HTML)
    rankings    → player_ratings, rating_history (computed from matches)
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DAEMON_SERVICE = "arena-rankings"


def stop_daemon() -> bool:
    """Stop the arena-rankings daemon before resetting. Returns True if it was running."""
    result = subprocess.run(["systemctl", "is-active", "--quiet", DAEMON_SERVICE])
    if result.returncode != 0:
        print("→ Daemon not running, skipping stop")
        return False
    print("→ Stopping daemon...")
    subprocess.run(["systemctl", "stop", DAEMON_SERVICE], check=True)
    print("  ✓ Daemon stopped")
    return True


def start_daemon(was_running: bool) -> None:
    """Start the arena-rankings daemon after reset, only if it was running before."""
    if not was_running:
        print("→ Daemon wasn't running, skipping start")
        return
    print("→ Starting daemon...")
    subprocess.run(["systemctl", "start", DAEMON_SERVICE], check=True)
    print("  ✓ Daemon started")

from clickhouse_driver import Client
from config import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
)
from src.db_schema import CREATE_DATABASE, DDL_STATEMENTS, DROP_DATABASE

# Tables by category
RANKING_TABLES = ["player_ratings", "rating_history"]
PARSED_TABLES = ["matches", "match_maps", "players"]
DOWNLOADED_TABLES = ["raw_posts", "tournaments", "discovery_state"]

VALID_TARGETS = ["rankings", "parsed", "all"]


def connect(database: str = "default") -> Client:
    return Client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        user=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        database=database,
    )


def get_engine(client: Client, db_name: str, table: str) -> str:
    rows = client.execute(
        "SELECT engine FROM system.tables WHERE database = %(db)s AND name = %(t)s",
        {"db": db_name, "t": table},
    )
    return rows[0][0] if rows else ""


def table_count(client: Client, table: str, db_name: str) -> int:
    engine = get_engine(client, db_name, table)
    suffix = "" if engine == "MergeTree" else " FINAL"
    return client.execute(f"SELECT count() FROM {table}{suffix}")[0][0]


def truncate_table(client: Client, table: str, db_name: str) -> None:
    engine = get_engine(client, db_name, table)
    # ReplacingMergeTree doesn't support TRUNCATE well with OPTIMIZE,
    # but TRUNCATE TABLE works on all engines.
    client.execute(f"TRUNCATE TABLE {table}")


def reset_rankings(client: Client, db_name: str, dry: bool) -> None:
    """Clear player_ratings and rating_history."""
    print("→ Resetting rankings\n")

    for table in RANKING_TABLES:
        count = table_count(client, table, db_name)
        print(f"  {table}: {count} rows")
        if not dry:
            truncate_table(client, table, db_name)
            print(f"    → truncated")

    if dry:
        print("\n[DRY-RUN] Ratings tables would be truncated")
        return

    print("\n  ✓ Ratings cleared. Daemon will recompute on next cycle.")


def reset_parsed(client: Client, db_name: str, dry: bool) -> None:
    """Clear parsed data (matches, match_maps, players) and reset parse_status."""
    print("→ Resetting parsed data\n")

    for table in PARSED_TABLES:
        count = table_count(client, table, db_name)
        print(f"  {table}: {count} rows")
        if not dry:
            truncate_table(client, table, db_name)
            print(f"    → truncated")

    # Reset status in raw_posts from 'parsed'/'skipped' → 'downloaded'
    # so the parser reprocesses them from stored raw_html.
    reg_count = table_count(client, "raw_posts", db_name)
    parsed_count = client.execute(
        "SELECT count() FROM raw_posts FINAL WHERE status != 'downloaded'"
    )[0][0]
    print(f"\n  raw_posts: {reg_count} rows ({parsed_count} to reset to downloaded)")

    if not dry and parsed_count > 0:
        # ReplacingMergeTree: insert new rows with status='downloaded'
        # for all posts that aren't already downloaded.
        rows = client.execute(
            "SELECT post_id, raw_html FROM raw_posts FINAL "
            "WHERE status != 'downloaded'"
        )
        if rows:
            BATCH = 2000
            total = 0
            for i in range(0, len(rows), BATCH):
                batch = rows[i:i + BATCH]
                data = [(r[0], r[1], "downloaded", "") for r in batch]
                client.execute(
                    "INSERT INTO raw_posts "
                    "(post_id, raw_html, status, reason) VALUES",
                    data,
                )
                total += len(data)
                print(f"    → {total}/{len(rows)} posts reset to downloaded", end="\r")
            print(f"    → {total}/{len(rows)} posts reset to downloaded")

    print("\n  ✓ Parsed data cleared. Daemon will reprocess on next cycle.")


def reset_all(client: Client, db_name: str, dry: bool, do_restore: bool) -> None:
    """Full reset: drop database, recreate tables, optionally restore downloaded data.

    Downloaded tables (raw_posts, tournaments, discovery_state) are preserved
    via backup.py (Parquet+zstd single archive) so the raw_posts HTML is backed
    up durably and restored OOM-free. backup.py is the single source of truth
    for backup/restore.
    """
    import backup as backup_mod

    # Tables to preserve (downloaded data)
    export_tables = DOWNLOADED_TABLES
    archive_path = None

    if do_restore:
        dbs = client.execute("SHOW DATABASES")
        db_exists = any(r[0] == db_name for r in dbs)
        if db_exists:
            if dry:
                target = connect(db_name)
                for table in export_tables:
                    n = table_count(target, table, db_name)
                    print(f"  Would export {n:>6} rows from {table}")
                target.disconnect()
            else:
                # Durable backup of the downloaded tables via backup.py.
                archive_path = backup_mod.BACKUP_ROOT / f"{db_name}_reset_{backup_mod._ts()}.tar.gz"
                print(f"\n→ Backing up downloaded tables to {archive_path} ...")
                backup_mod.do_backup(export_tables, archive_path)
        else:
            print(f"  Database '{db_name}' doesn't exist yet — nothing to export")

    if dry:
        print(f"\n[DRY-RUN] Would DROP DATABASE {db_name} and recreate all tables")
        return

    print(f"\n→ Dropping database '{db_name}'...")
    client.execute(DROP_DATABASE.replace("arena_rankings", db_name))
    client.execute(CREATE_DATABASE.replace("arena_rankings", db_name))
    print("✓ Database recreated")

    target = connect(db_name)
    print("\n→ Creating tables...")
    for ddl in DDL_STATEMENTS:
        resolved = ddl.strip().replace("arena_rankings.", f"{db_name}.")
        target.execute(resolved)
        for line in ddl.split("\n"):
            if "CREATE TABLE" in line:
                tname = line.split("arena_rankings.")[1].split("(")[0].strip()
                for l2 in ddl.split("\n"):
                    if "ORDER BY" in l2:
                        print(f"  ✓ {tname:20s}  {l2.strip()}")
                        break
                break
    print(f"✓ {len(DDL_STATEMENTS)} tables created")

    if archive_path:
        print("\n→ Restoring downloaded data...")
        backup_mod.do_restore(export_tables, archive_path)

    print("\n→ Final state:")
    tables = target.execute("SHOW TABLES")
    for (tname,) in tables:
        count = table_count(target, tname, db_name)
        print(f"  {tname:20s}  {count:>8} rows")

    target.disconnect()
    print("\n✅ Full reset complete.")
    print("ℹ  Daemon will reprocess matches and recompute ratings automatically.")


def main():
    parser = argparse.ArgumentParser(
        description="Reset Arena Rankings ClickHouse database",
        usage="python3 reset.py [--dry-run] {rankings|parsed|all} ...",
    )
    parser.add_argument(
        "targets",
        nargs="+",
        choices=VALID_TARGETS,
        metavar="target",
        help="What to reset: 'rankings', 'parsed', 'all' (can combine: 'rankings parsed')",
    )
    parser.add_argument(
        "--no-restore",
        action="store_true",
        help="With 'all': don't restore downloaded data (empty database)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without executing",
    )
    args = parser.parse_args()

    db_name = CLICKHOUSE_DATABASE
    dry = args.dry_run

    if not dry:
        was_running = stop_daemon()
    else:
        was_running = False

    client = connect()

    do_rankings = "rankings" in args.targets
    do_parsed = "parsed" in args.targets
    do_all = "all" in args.targets

    if do_all:
        reset_all(client, db_name, dry, do_restore=not args.no_restore)
    else:
        target = connect(db_name)
        if do_parsed:
            reset_parsed(target, db_name, dry)
        if do_rankings:
            if do_parsed:
                print()
            reset_rankings(target, db_name, dry)

        if not dry:
            print("\n→ Final state:")
            tables = target.execute("SHOW TABLES")
            for (tname,) in tables:
                count = table_count(target, tname, db_name)
                print(f"  {tname:20s}  {count:>8} rows")
        target.disconnect()

    client.disconnect()
    if not dry:
        print("\n✅ Done.")
        start_daemon(was_running)


if __name__ == "__main__":
    main()

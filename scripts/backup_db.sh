#!/usr/bin/env bash
# Logical backup of the arena_rankings ClickHouse database.
# Writes per-table data files (escaped TabSeparated) + a DDL file into a
# timestamped directory under /opt/arena-rankings/backups/
#
# Restore:
#   sed 's/arena_rankings\./arena_rankings_test./g' ddls.sql | \
#     docker exec -i quake-ch clickhouse-client --multiquery
#   docker exec -i quake-ch clickhouse-client \
#     --query "INSERT INTO <db>.<t> FORMAT TabSeparated" < <t>.tsv
#
# TabSeparated escapes embedded newlines/tabs/backslashes inside string values
# (raw_html, rankings, maplist), so it round-trips HTML that contains them.
set -euo pipefail

CH="quake-ch"
DB="arena_rankings"
TS="$(date +%Y%m%d_%H%M%S)"
DIR="/opt/arena-rankings/backups/${DB}_${TS}"
mkdir -p "$DIR"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Backing up database '${DB}' to ${DIR} ..."

# 1) DDL for every table, unescaped into replayable SQL.
: > "$DIR/ddls.sql"
docker exec "$CH" clickhouse-client --database "$DB" --query "SHOW TABLES" | while read -r t; do
  {
    echo "DROP TABLE IF EXISTS ${DB}.${t};"
    docker exec "$CH" clickhouse-client --database "$DB" --query "SHOW CREATE TABLE ${DB}.${t}"
    echo ""
  } >> "$DIR/ddls.sql"
done
python3 "$SCRIPT_DIR/unescape_ddl.py" "$DIR/ddls.sql"

# 2) Row data per table (TabSeparated escapes embedded newlines/tabs).
docker exec "$CH" clickhouse-client --database "$DB" --query "SHOW TABLES" | while read -r t; do
  echo "  dumping: ${t}"
  docker exec "$CH" clickhouse-client --database "$DB" --query \
    "SELECT * FROM ${DB}.${t} FORMAT TabSeparated" > "$DIR/${t}.tsv"
done

# 3) Manifest
{
  echo "Arena Rankings ClickHouse logical backup"
  echo "Created:  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "Database: ${DB}  |  Container: ${CH}"
  echo "Tables:"
  for f in "$DIR"/*.tsv; do
    [ -e "$f" ] || continue
    t=$(basename "$f" .tsv)
    echo "  ${t}: $(wc -l < "$f") rows"
  done
} > "$DIR/MANIFEST.txt"

echo "Backup complete: $DIR"
du -sh "$DIR"
cat "$DIR/MANIFEST.txt"

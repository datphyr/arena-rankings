#!/usr/bin/env python3
"""Unescape ClickHouse `SHOW CREATE TABLE` output into replayable DDL.

The backup script captures SHOW CREATE TABLE via clickhouse-client, which emits
the whole CREATE statement as a single line with literal \\n, \\t, \\', \\\\ escapes
(for string literal contents) and no trailing semicolon. This script rewrites
that file into valid, replayable SQL (one statement per block, each ending in ';').
"""
import os
import sys


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BACKUP_DDL")
    if not path:
        print("usage: unescape_ddl.py <ddl.sql>", file=sys.stderr)
        return 2

    with open(path) as f:
        s = f.read()

    # Unescape the single-line string form emitted by SHOW CREATE TABLE.
    s = (
        s.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\'", "'")
        .replace("\\\\", "\\")
    )

    # Ensure each CREATE statement ends with ';' for --multiquery replay.
    # Statements are separated by "DROP TABLE IF EXISTS ..." lines.
    parts = s.split("DROP TABLE")
    s = "".join(("DROP TABLE" + p.rstrip() + ";") if i else p for i, p in enumerate(parts))

    with open(path, "w") as f:
        f.write(s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

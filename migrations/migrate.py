"""Applies the numbered SQL migrations in this directory to the SQLite database.

Run by the init container (python migrations/migrate.py) and by `python -m serendip migrate`.
Safe to run again: applied versions are recorded in schema_version and skipped.
"""

import os
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent


def migrate(db_path: str, directory: Path = HERE) -> list[str]:
    """Applies the migrations not yet recorded and returns the names it applied."""
    conn = sqlite3.connect(db_path)
    applied_now: list[str] = []
    try:
        conn.execute("pragma journal_mode = wal")
        conn.execute(
            "create table if not exists schema_version ("
            " version integer primary key, name text not null,"
            " applied_at text not null default (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))"
        )
        applied = {r[0] for r in conn.execute("select version from schema_version")}

        for path in sorted(directory.glob("[0-9]*.sql")):
            version = int(re.match(r"\d+", path.name).group())
            if version in applied:
                continue
            # executescript commits first, so each file is wrapped in its own transaction.
            conn.executescript(f"begin;\n{path.read_text()}\n")
            conn.execute(
                "insert into schema_version (version, name) values (?, ?)", (version, path.name)
            )
            conn.commit()
            applied_now.append(path.name)
    finally:
        conn.close()
    return applied_now


def main() -> int:
    db_path = os.environ.get("SERENDIP_DB_PATH", "/data/serendip.db")
    applied = migrate(db_path)
    for name in applied:
        print(f"applied {name}")
    print(f"db at {db_path}, {len(applied)} migration(s) applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())

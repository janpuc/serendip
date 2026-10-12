"""Applies the numbered SQL migrations in serendip/migrations/ to the SQLite database.

Run by the init container as `python -m serendip migrate`. The SQL ships inside the package,
so it works from an installed wheel. Safe to run again: applied versions are recorded in
schema_version and skipped.
"""

import re
import sqlite3
from importlib.resources import files
from importlib.resources.abc import Traversable

MIGRATIONS = files("serendip") / "migrations"


def migrate(db_path: str, directory: Traversable = MIGRATIONS) -> list[str]:
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

        paths = sorted(
            (p for p in directory.iterdir() if re.match(r"\d+.*\.sql$", p.name)),
            key=lambda p: p.name,
        )
        for path in paths:
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

"""python -m serendip migrate | serve | subs reset --user <name>"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
from pathlib import Path

from . import logs
from .config import Settings
from .db import Database

MIGRATE_PY = Path(__file__).resolve().parents[2] / "migrations" / "migrate.py"


def _migrate(settings: Settings) -> int:
    # migrations/ sits beside the package, not inside it, so the init container can run it alone.
    spec = importlib.util.spec_from_file_location("serendip_migrate", MIGRATE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in module.migrate(settings.db_path):
        print(f"applied {name}")
    return 0


async def _subs_reset(settings: Settings, name: str) -> int:
    db = await Database.connect(settings.db_path)
    try:
        removed = await db.reset_subscriptions(name)
    finally:
        await db.close()
    print(f"removed {removed} subscription(s) for user {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="serendip")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply SQL migrations")
    sub.add_parser("serve", help="run the ingest and API servers")
    subs = sub.add_parser("subs", help="observed subscriptions")
    subs_sub = subs.add_subparsers(dest="subs_command", required=True)
    reset = subs_sub.add_parser("reset", help="forget a user's observed subscriptions")
    reset.add_argument("--user", required=True, help="user name from SERENDIP_USERS")
    args = parser.parse_args(argv)

    settings = Settings()
    logs.configure(settings.log_level)
    if args.command == "migrate":
        return _migrate(settings)
    if args.command == "serve":
        from .server import serve

        return asyncio.run(serve(settings))
    return asyncio.run(_subs_reset(settings, args.user))

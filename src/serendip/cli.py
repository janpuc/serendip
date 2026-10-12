"""python -m serendip migrate | serve | subs reset --user <name> | breaker reset | breaker trip"""

from __future__ import annotations

import argparse
import asyncio

from . import logs
from .breaker import MANUAL, Breaker
from .config import Settings
from .db import Database
from .events import clean_text
from .migrate import migrate


def _migrate(settings: Settings) -> int:
    for name in migrate(settings.db_path):
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


async def _breaker(settings: Settings, command: str, reason: str | None) -> int:
    """Works on the database file, so it takes effect in a running server: the server reads the
    breaker before every call."""
    db = await Database.connect(settings.db_path)
    try:
        breaker = Breaker(db)
        if command == "reset":
            changed = await breaker.reset()
            print("breaker reset" if changed else "breaker was not open")
        else:
            assert reason is not None
            changed = await breaker.trip(MANUAL, clean_text(reason, 200) or "manual")
            print("breaker tripped" if changed else "breaker was already open")
        state = await breaker.state()
        print(f"open={state.open} kind={state.kind} reason={state.reason}")
    finally:
        await db.close()
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
    breaker = sub.add_parser("breaker", help="the switch that stops calls to Yattee Server")
    breaker_sub = breaker.add_subparsers(dest="breaker_command", required=True)
    breaker_sub.add_parser("reset", help="close the breaker and resume follow-ups and feed posts")
    trip = breaker_sub.add_parser("trip", help="open the breaker by hand")
    trip.add_argument("--reason", required=True, help="why, shown in /status")
    args = parser.parse_args(argv)

    settings = Settings()
    logs.configure(settings.log_level)
    if args.command == "migrate":
        return _migrate(settings)
    if args.command == "serve":
        from .server import serve

        return asyncio.run(serve(settings))
    if args.command == "breaker":
        return asyncio.run(_breaker(settings, args.breaker_command, getattr(args, "reason", None)))
    return asyncio.run(_subs_reset(settings, args.user))

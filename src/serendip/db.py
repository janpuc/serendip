"""SQLite. Single writer, single connection, WAL.

All SQL lives here. The schema comes from migrations/, which the init container applies.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import aiosqlite

RETENTION_DAYS = 180


def stamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn
        # The consumer runs several tasks over one connection; a transaction is one at a time.
        self._write = asyncio.Lock()

    @classmethod
    async def connect(cls, path: str) -> Database:
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("pragma journal_mode = wal")
        await conn.execute("pragma foreign_keys = on")
        await conn.execute("pragma busy_timeout = 5000")
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    @contextlib.asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        """One writer at a time; commits on success, rolls back on any BaseException."""
        async with self._write:
            try:
                yield
                await self._conn.commit()
            except BaseException:
                # Shielded: a cancelled task must still leave no half-written transaction.
                await asyncio.shield(self._conn.rollback())
                raise

    async def _upsert_user(self, name: str, ts: str) -> int:
        """Returns the internal id of the configured user `name`."""
        cur = await self._conn.execute(
            "insert into users (name, first_seen) values (?, ?)"
            " on conflict (name) do update set first_seen = min(first_seen, excluded.first_seen)"
            " returning id",
            (name, ts),
        )
        return (await cur.fetchone())[0]

    async def _insert_event(
        self,
        user_id: int,
        ts: str,
        device: str,
        kind: str,
        video_id: str | None = None,
        query: str | None = None,
    ) -> None:
        await self._conn.execute(
            "insert into events (user_id, ts_utc, device, kind, video_id, query)"
            " values (?, ?, ?, ?, ?, ?)",
            (user_id, ts, device, kind, video_id, query),
        )

    async def record_open(
        self, name: str, device: str, ts: str, video_id: str, query: str | None
    ) -> None:
        async with self._transaction():
            user_id = await self._upsert_user(name, ts)
            await self._insert_event(user_id, ts, device, "open", video_id, query)
            await self._conn.execute(
                "insert into opens (user_id, video_id, last_open_utc, count) values (?, ?, ?, 1)"
                " on conflict (user_id, video_id) do update set"
                " count = count + 1, last_open_utc = max(last_open_utc, excluded.last_open_utc)",
                (user_id, video_id, ts),
            )

    async def record_search(self, name: str, device: str, ts: str, query: str) -> None:
        async with self._transaction():
            user_id = await self._upsert_user(name, ts)
            await self._insert_event(user_id, ts, device, "search", query=query)

    async def record_subscriptions(
        self,
        name: str,
        device: str,
        ts: str,
        channels: list[tuple[str, str | None]],
    ) -> None:
        """Adds (channel_id, name) pairs to the user's set. Nothing is ever removed here.

        Consumers can apply events out of order, so times only widen: first_seen never moves
        later and last_seen_by_user never moves earlier. A name from an older event does not
        replace a newer one.
        """
        async with self._transaction():
            user_id = await self._upsert_user(name, ts)
            await self._insert_event(user_id, ts, device, "subscriptions_seen")
            await self._conn.executemany(
                "insert into subscriptions"
                " (user_id, channel_id, name, first_seen, last_seen_by_user)"
                " values (?, ?, ?, ?, ?)"
                " on conflict (user_id, channel_id) do update set"
                " name = case when excluded.last_seen_by_user >= last_seen_by_user"
                " then coalesce(excluded.name, name) else coalesce(name, excluded.name) end,"
                " first_seen = min(first_seen, excluded.first_seen),"
                " last_seen_by_user = max(last_seen_by_user, excluded.last_seen_by_user)",
                [(user_id, cid, cname, ts, ts) for cid, cname in channels],
            )

    async def reset_subscriptions(self, name: str) -> int:
        async with self._transaction():
            cur = await self._conn.execute(
                "delete from subscriptions where user_id = (select id from users where name = ?)",
                (name,),
            )
        return cur.rowcount

    async def delete_events_before(self, cutoff: datetime) -> int:
        async with self._transaction():
            cur = await self._conn.execute("delete from events where ts_utc < ?", (stamp(cutoff),))
        return cur.rowcount

    async def apply_retention(self) -> int:
        return await self.delete_events_before(datetime.now(UTC) - timedelta(days=RETENTION_DAYS))

    async def checkpoint(self) -> None:
        async with self._write:
            await self._conn.execute("pragma wal_checkpoint(truncate)")

    async def counts(self) -> dict[str, int]:
        out = {}
        for table in ("users", "events", "subscriptions", "opens"):
            async with self._conn.execute(f"select count(*) from {table}") as cur:
                out[table] = (await cur.fetchone())[0]
        return out

    async def last_event_at(self) -> str | None:
        async with self._conn.execute("select max(ts_utc) from events") as cur:
            row = await cur.fetchone()
        return row[0] if row else None

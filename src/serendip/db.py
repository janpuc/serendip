"""SQLite. Single writer, single connection, WAL.

All SQL lives here, except the list-building queries in lists.py, which run inside one
transaction opened by Database.immediate(). The schema comes from serendip/migrations/, which the
init container applies.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from .catalog import VideoRecord

RETENTION_DAYS = 180
# Impressions, lists and follow-up rows (DESIGN 9).
SHORT_RETENTION_DAYS = 30
# Subscriptions count for the feed and for "followed" while seen this recently (DESIGN 4).
ACTIVE_SUBSCRIPTION_DAYS = 30
# Related items expire this long after they were found; subs items after their upload.
POOL_EXPIRY_DAYS = 30


def stamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp_us(dt: datetime) -> str:
    """With microseconds, for lists and impressions."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_stamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@dataclass(frozen=True)
class BreakerState:
    open: bool
    kind: str | None = None
    reason: str | None = None
    tripped_at: str | None = None


@dataclass(frozen=True)
class FollowupSeen:
    """What earlier follow-ups say about a video id. A reservation whose send was skipped
    (breaker, deadline, clock) asked the server for nothing: it counts in the budget but neither
    marks the video failed nor counts as fetched."""

    failed_before: bool
    reserved_last: str | None


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

    async def _rollback(self) -> None:
        """Ends whatever transaction the connection has; harmless when none was started."""
        with contextlib.suppress(sqlite3.OperationalError):
            await self._conn.rollback()

    @contextlib.asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        """One writer at a time; commits on success, rolls back on any BaseException."""
        async with self._write:
            try:
                yield
                await self._conn.commit()
            except BaseException:
                # Shielded: a cancelled task must still leave no half-written transaction.
                await asyncio.shield(self._rollback())
                raise

    @contextlib.asynccontextmanager
    async def immediate(self) -> AsyncIterator[aiosqlite.Connection]:
        """A transaction that holds the write lock from before its first read.

        `begin immediate` takes SQLite's write lock at once, and the asyncio lock keeps every other
        task on this connection out, so a read-decide-write sequence cannot interleave with
        another one. Commits on success, rolls back on any BaseException. `begin immediate` is
        inside the protected block: aiosqlite runs a queued statement even when the task awaiting
        it is cancelled, so a cancellation there can still leave a transaction open.
        """
        async with self._write:
            try:
                await self._conn.execute("begin immediate")
                yield self._conn
                await self._conn.commit()
            except BaseException:
                await asyncio.shield(self._rollback())
                raise

    @contextlib.asynccontextmanager
    async def reading(self) -> AsyncIterator[aiosqlite.Connection]:
        """Reads that must not see another task's uncommitted rows."""
        async with self._write:
            yield self._conn

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
                "insert into opens (user_id, video_id, channel_id, last_open_utc, count)"
                " values (?, ?, (select author_id from videos where video_id = ?), ?, 1)"
                " on conflict (user_id, video_id) do update set"
                " count = count + 1, last_open_utc = max(last_open_utc, excluded.last_open_utc),"
                " channel_id = coalesce(channel_id, excluded.channel_id)",
                (user_id, video_id, video_id, ts),
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

    # --- breaker (DESIGN 7) ------------------------------------------------------------------

    async def breaker_state(self) -> BreakerState:
        async with self._conn.execute(
            "select open, kind, reason, tripped_at from breaker where id = 1"
        ) as cur:
            row = await cur.fetchone()
        return BreakerState(bool(row["open"]), row["kind"], row["reason"], row["tripped_at"])

    async def trip_breaker(self, kind: str, reason: str, ts: str) -> bool:
        """Opens the breaker. Returns False, and keeps the first reason, if it was open already."""
        async with self._transaction():
            cur = await self._conn.execute(
                "update breaker set open = 1, kind = ?, reason = ?, tripped_at = ?"
                " where id = 1 and open = 0",
                (kind, reason, ts),
            )
        return cur.rowcount == 1

    async def initialise_breaker(self, start_open: bool, kind: str, reason: str, ts: str) -> bool:
        """The first start with this breaker row: marks it initialised and, if `start_open`, opens
        it. Returns True if it opened. Later starts change nothing."""
        async with self._transaction():
            cur = await self._conn.execute(
                "update breaker set initialised = 1 where id = 1 and initialised = 0"
            )
            first = cur.rowcount == 1
            if first and start_open:
                await self._conn.execute(
                    "update breaker set open = 1, kind = ?, reason = ?, tripped_at = ?"
                    " where id = 1 and open = 0",
                    (kind, reason, ts),
                )
        return first and start_open

    async def reset_breaker(self) -> bool:
        async with self._transaction():
            cur = await self._conn.execute(
                "update breaker set open = 0, kind = null, reason = null, tripped_at = null"
                " where id = 1 and open = 1"
            )
        return cur.rowcount == 1

    # --- follow-ups (DESIGN 5.1, 7) ----------------------------------------------------------

    async def followup_reserve(self, video_id: str, ts: str) -> int:
        """Written before the request is sent; it counts whatever happens next."""
        async with self._transaction():
            cur = await self._conn.execute(
                "insert into followups (video_id, reserved_at, status) values (?, ?, 'reserved')",
                (video_id, ts),
            )
        return cur.lastrowid

    async def followup_mark_sent(self, followup_id: int, ts: str) -> None:
        """Written just before the request goes to the client's gate, which can still skip it."""
        async with self._transaction():
            await self._conn.execute(
                "update followups set sent_at = ?, status = 'sent' where id = ?", (ts, followup_id)
            )

    async def followup_finish(
        self,
        followup_id: int,
        ts: str,
        status: str,
        *,
        seconds: float | None,
        extraction_method: str | None,
        outcome: str | None,
        related_count: int | None,
    ) -> None:
        async with self._transaction():
            await self._conn.execute(
                "update followups set finished_at = ?, status = ?, seconds = ?,"
                " extraction_method = ?, outcome = ?, related_count = ?,"
                " sent_at = case when ? = 'skipped' then null else sent_at end where id = ?",
                (
                    ts,
                    status,
                    seconds,
                    extraction_method,
                    outcome,
                    related_count,
                    status,
                    followup_id,
                ),
            )

    async def interrupt_followups(self, ts: str) -> int:
        """Marks reservations a crash left unfinished. They stay counted and are never replayed."""
        async with self._transaction():
            cur = await self._conn.execute(
                "update followups set status = 'interrupted', finished_at = ?"
                " where status in ('reserved', 'sent')",
                (ts,),
            )
        return cur.rowcount

    async def followup_seen(self, video_id: str) -> FollowupSeen:
        async with self._conn.execute(
            "select max(case when status != 'skipped' then reserved_at end),"
            " coalesce(max(status not in ('ok', 'skipped')), 0) from followups where video_id = ?",
            (video_id,),
        ) as cur:
            last, failed = await cur.fetchone()
        return FollowupSeen(bool(failed), last)

    async def followup_reservations_since(self, since: str) -> list[str]:
        """Reservation times at or after `since`, newest first."""
        async with self._conn.execute(
            "select reserved_at from followups where reserved_at >= ? order by reserved_at desc",
            (since,),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]

    async def last_followup(self) -> dict | None:
        async with self._conn.execute(
            "select video_id, reserved_at, finished_at, status, seconds, extraction_method,"
            " outcome, related_count from followups order by id desc limit 1"
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        out = dict(row)
        out.pop("video_id")  # user data
        return out

    async def is_known_live(self, video_id: str) -> bool:
        async with self._conn.execute(
            "select is_live or is_upcoming from videos where video_id = ?", (video_id,)
        ) as cur:
            row = await cur.fetchone()
        return bool(row and row[0])

    # --- feed (DESIGN 5) ---------------------------------------------------------------------

    async def active_subscriptions(
        self, name: str, since: datetime
    ) -> list[tuple[str, str | None]]:
        async with self._conn.execute(
            "select channel_id, s.name from subscriptions s join users u on u.id = s.user_id"
            " where u.name = ? and s.last_seen_by_user >= ? order by channel_id",
            (name, stamp(since)),
        ) as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]

    async def users_with_subscriptions(self, since: datetime) -> list[str]:
        async with self._conn.execute(
            "select distinct u.name from users u join subscriptions s on s.user_id = u.id"
            " where s.last_seen_by_user >= ? order by u.name",
            (stamp(since),),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]

    async def last_feed_started(self) -> str | None:
        """When the newest feed call, for any user, was reserved."""
        async with self._conn.execute("select max(started_at) from feed_runs") as cur:
            return (await cur.fetchone())[0]

    async def feed_cursor(self) -> tuple[str, int] | None:
        """(user name, chunk offset) where the next feed call starts."""
        async with self._conn.execute(
            "select user_name, chunk_offset from feed_cursor where id = 1"
        ) as cur:
            row = await cur.fetchone()
        return (row[0], row[1]) if row else None

    async def feed_reserve(
        self,
        name: str,
        ts: str,
        chunk_offset: int,
        channels: int,
        next_name: str,
        next_offset: int,
    ) -> int:
        """The reservation for one feed call and the cursor move, in one transaction, written
        before the POST is sent. It counts whatever happens next, a crash included."""
        async with self._transaction():
            user_id = await self._upsert_user(name, ts)
            cur = await self._conn.execute(
                "insert into feed_runs (user_id, started_at, status, chunk_offset, channels)"
                " values (?, ?, 'fetching', ?, ?)",
                (user_id, ts, chunk_offset, channels),
            )
            await self._conn.execute(
                "insert into feed_cursor (id, user_name, chunk_offset) values (1, ?, ?)"
                " on conflict (id) do update set user_name = excluded.user_name,"
                " chunk_offset = excluded.chunk_offset",
                (next_name, next_offset),
            )
        return cur.lastrowid

    async def feed_run_finish(
        self, run_id: int, ts: str, status: str, items: int, truncated: bool
    ) -> None:
        async with self._transaction():
            await self._conn.execute(
                "update feed_runs set finished_at = ?, status = ?, items = ?, truncated = ?"
                " where id = ?",
                (ts, status, items, int(truncated), run_id),
            )

    async def last_feed_runs(self) -> list[dict]:
        """The latest run of each user, without user names."""
        async with self._conn.execute(
            "select started_at, finished_at, status, chunk_offset, channels, items, truncated"
            " from feed_runs"
            " where id in (select max(id) from feed_runs group by user_id) order by started_at desc"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # --- catalog and pool (DESIGN 5, 9) ------------------------------------------------------

    async def _upsert_videos(
        self, records: list[VideoRecord], found_at: str, *, exact_published: bool
    ) -> None:
        # A feed item knows its upload time; a related item only estimates it from text, so it
        # never replaces one that is already there.
        published = (
            "coalesce(excluded.published_est, videos.published_est)"
            if exact_published
            else "coalesce(videos.published_est, excluded.published_est)"
        )
        await self._conn.executemany(
            "insert into videos (video_id, title, author, author_id, length_s, published_est,"
            " published_text, view_count, view_count_text, is_live, is_upcoming, is_short,"
            " found_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " on conflict (video_id) do update set title = excluded.title,"
            " author = excluded.author, author_id = excluded.author_id,"
            " length_s = excluded.length_s,"
            f" published_est = {published},"
            " published_text = coalesce(excluded.published_text, videos.published_text),"
            " view_count = coalesce(excluded.view_count, videos.view_count),"
            " view_count_text = coalesce(excluded.view_count_text, videos.view_count_text),"
            " is_live = excluded.is_live, is_upcoming = excluded.is_upcoming,"
            " is_short = coalesce(excluded.is_short, videos.is_short)",
            [
                (
                    r.video_id,
                    r.title,
                    r.author,
                    r.author_id,
                    r.length_s,
                    stamp(r.published_est) if r.published_est else None,
                    r.published_text,
                    r.view_count,
                    r.view_count_text,
                    int(r.is_live),
                    int(r.is_upcoming),
                    1 if r.is_short else None,
                    found_at,
                )
                for r in records
            ],
        )
        # Opens learn their channel when serendip learns the video's.
        await self._conn.execute(
            "update opens set channel_id = (select author_id from videos v"
            " where v.video_id = opens.video_id)"
            " where channel_id is null and video_id in (select video_id from videos)"
        )

    async def _upsert_pool(
        self, user_id: int, entries: list[tuple[str, str, str | None, str]], found_at: str
    ) -> None:
        """entries: (video_id, source, seed_video_id, expires_at)."""
        await self._conn.executemany(
            "insert into pool (user_id, video_id, source, seed_video_id, found_at, expires_at)"
            " values (?, ?, ?, ?, ?, ?)"
            " on conflict (user_id, video_id) do update set"
            # A video in both the feed and a related list counts as subs.
            " source = case when pool.source = 'subs' or excluded.source = 'subs' then 'subs'"
            " else excluded.source end,"
            " seed_video_id = case when excluded.source = 'subs' or pool.source = 'subs' then null"
            " else coalesce(pool.seed_video_id, excluded.seed_video_id) end,"
            " expires_at = case when excluded.source = 'subs' then excluded.expires_at"
            " when pool.source = 'subs' then pool.expires_at"
            " else max(pool.expires_at, excluded.expires_at) end",
            [(user_id, vid, source, seed, found_at, exp) for vid, source, seed, exp in entries],
        )

    async def store_feed(self, name: str, now: datetime, records: list[VideoRecord]) -> int:
        """Subscription uploads into the catalog and the user's `subs` pool. Returns pool rows."""
        found_at = stamp(now)
        entries = []
        kept = []
        for r in records:
            expires = (r.published_est or now) + timedelta(days=POOL_EXPIRY_DAYS)
            if expires > now:
                kept.append(r)
                entries.append((r.video_id, "subs", None, stamp(expires)))
        async with self._transaction():
            user_id = await self._upsert_user(name, found_at)
            await self._upsert_videos(kept, found_at, exact_published=True)
            await self._upsert_pool(user_id, entries, found_at)
        return len(entries)

    async def store_related(
        self,
        names: list[str],
        now: datetime,
        seed_id: str,
        seed: VideoRecord | None,
        related: list[VideoRecord],
    ) -> int:
        """A follow-up's result: the seed's metadata and its related items as `related` (the
        channel is one the user follows) or `adjacent`. Returns the pool rows written."""
        found_at = stamp(now)
        expires = stamp(now + timedelta(days=POOL_EXPIRY_DAYS))
        related = [r for r in related if r.video_id != seed_id]
        written = 0
        async with self._transaction():
            records = ([seed] if seed else []) + related
            await self._upsert_videos(records, found_at, exact_published=False)
            for name in names:
                user_id = await self._upsert_user(name, found_at)
                async with self._conn.execute(
                    "select channel_id from subscriptions where user_id = ?"
                    " and last_seen_by_user >= ?",
                    (user_id, stamp(now - timedelta(days=ACTIVE_SUBSCRIPTION_DAYS))),
                ) as cur:
                    followed = {r[0] for r in await cur.fetchall()}
                entries = [
                    (
                        r.video_id,
                        "related" if r.author_id in followed else "adjacent",
                        seed_id,
                        expires,
                    )
                    for r in related
                ]
                await self._upsert_pool(user_id, entries, found_at)
                written += len(entries)
        return written

    # --- retention, status -------------------------------------------------------------------

    async def apply_discover_retention(self, now: datetime) -> dict[str, int]:
        """Pool, impressions, lists, follow-ups and feed runs expire; exposures are kept."""
        cutoff = now - timedelta(days=SHORT_RETENTION_DAYS)
        deleted = {}
        async with self._transaction():
            for label, sql, args in (
                ("pool", "delete from pool where expires_at <= ?", (stamp(now),)),
                (
                    "impressions",
                    "delete from impressions where ts_utc < ?",
                    (stamp_us(cutoff),),
                ),
                (
                    "lists",
                    "delete from lists where ts_utc < ? and id not in"
                    " (select list_id from impressions)",
                    (stamp_us(cutoff),),
                ),
                ("followups", "delete from followups where reserved_at < ?", (stamp_us(cutoff),)),
                ("feed_runs", "delete from feed_runs where started_at < ?", (stamp_us(cutoff),)),
                (
                    "videos",
                    "delete from videos where found_at < ?"
                    " and video_id not in (select video_id from pool)"
                    " and video_id not in (select video_id from impressions)",
                    (stamp(cutoff),),
                ),
            ):
                cur = await self._conn.execute(sql, args)
                deleted[label] = cur.rowcount
        return deleted

    async def pool_counts(self, now: datetime) -> dict[str, int]:
        async with self._conn.execute(
            "select source, count(*) from pool where expires_at > ? group by source",
            (stamp(now),),
        ) as cur:
            counts = {"subs": 0, "related": 0, "adjacent": 0}
            counts.update({r[0]: r[1] for r in await cur.fetchall()})
        return counts

    async def user_names(self) -> list[str]:
        async with self._conn.execute("select name from users order by name") as cur:
            return [r[0] for r in await cur.fetchall()]

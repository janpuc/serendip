"""Serving /api/v1/popular from the database (DESIGN 6, 6.1). No outbound call of any kind.

One transaction takes the write lock before it reads anything, selects, validates the response
items and records the list, the impressions and the exposure ledger. Two requests at the same
moment therefore cannot be handed the same never-served video, and a failure or a cancellation
rolls the lot back.
"""

from __future__ import annotations

import hashlib
import random
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiosqlite

from . import selection
from .catalog import StoredVideo, is_servable, to_list_item, validate_list_item
from .config import Settings
from .db import ACTIVE_SUBSCRIPTION_DAYS, Database, parse_stamp, stamp, stamp_us

OPENED_EXCLUSION_DAYS = 90
NOVELTY_HOURS = 24

_VIDEO_COLUMNS = (
    "v.video_id, v.title, v.author, v.author_id, v.length_s, v.published_est, v.published_text,"
    " v.view_count, v.view_count_text, v.is_live, v.is_upcoming, v.is_short, v.found_at"
)


@dataclass(frozen=True)
class Served:
    items: list[dict]
    kinds: Counter[str]
    repeat: bool


def _video(row: aiosqlite.Row) -> StoredVideo:
    return StoredVideo(
        video_id=row["video_id"],
        title=row["title"],
        author=row["author"],
        author_id=row["author_id"],
        length_s=row["length_s"],
        published_est=row["published_est"],
        published_text=row["published_text"],
        view_count=row["view_count"],
        view_count_text=row["view_count_text"],
        is_live=bool(row["is_live"]),
        is_upcoming=bool(row["is_upcoming"]),
        is_short=None if row["is_short"] is None else bool(row["is_short"]),
        found_at=row["found_at"],
    )


def _seed(user: str, now: datetime) -> int:
    digest = hashlib.sha256(f"{user}|{stamp_us(now)}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


async def _user_id(conn: aiosqlite.Connection, name: str, ts: str) -> int:
    cur = await conn.execute(
        "insert into users (name, first_seen) values (?, ?)"
        " on conflict (name) do update set first_seen = min(first_seen, excluded.first_seen)"
        " returning id",
        (name, ts),
    )
    return (await cur.fetchone())[0]


async def _column(conn: aiosqlite.Connection, sql: str, *args) -> list:
    async with conn.execute(sql, args) as cur:
        return [r[0] for r in await cur.fetchall()]


@dataclass(frozen=True)
class _Device:
    """The last two lists served on one device."""

    last_id: int | None = None
    last_ts: datetime | None = None
    last_positions: dict[str, int] | None = None
    before_last_ids: frozenset[str] = frozenset()


async def _device_history(conn: aiosqlite.Connection, user_id: int, device: str) -> _Device:
    async with conn.execute(
        "select id, ts_utc from lists where user_id = ? and device = ?"
        " order by ts_utc desc, id desc limit 2",
        (user_id, device),
    ) as cur:
        lists = await cur.fetchall()
    if not lists:
        return _Device()
    async with conn.execute(
        "select video_id, position from impressions where list_id = ?", (lists[0]["id"],)
    ) as cur:
        positions = {r["video_id"]: r["position"] for r in await cur.fetchall()}
    before = frozenset()
    if len(lists) > 1:
        before = frozenset(
            await _column(
                conn, "select video_id from impressions where list_id = ?", lists[1]["id"]
            )
        )
    return _Device(lists[0]["id"], parse_stamp(lists[0]["ts_utc"]), positions, before)


async def _repeat_list(
    conn: aiosqlite.Connection, user_id: int, history: _Device
) -> list[StoredVideo]:
    """The stored list minus what was opened since (compared to the second, so a tie drops it)."""
    assert history.last_id is not None and history.last_ts is not None
    since = stamp(history.last_ts)
    opened = set(
        await _column(
            conn,
            "select video_id from opens where user_id = ? and last_open_utc >= ?",
            user_id,
            since,
        )
    )
    async with conn.execute(
        f"select {_VIDEO_COLUMNS} from impressions i join videos v on v.video_id = i.video_id"
        " where i.list_id = ? order by i.position",
        (history.last_id,),
    ) as cur:
        return [_video(r) for r in await cur.fetchall() if r["video_id"] not in opened]


async def _open_boosts(conn: aiosqlite.Connection, user_id: int, now: datetime) -> dict[str, float]:
    boosts: dict[str, float] = {}
    async with conn.execute(
        "select channel_id, last_open_utc from opens where user_id = ? and channel_id is not null",
        (user_id,),
    ) as cur:
        for row in await cur.fetchall():
            age_days = max((now - parse_stamp(row["last_open_utc"])).total_seconds() / 86400, 0)
            boosts[row["channel_id"]] = boosts.get(row["channel_id"], 0.0) + 0.5 ** (
                age_days / selection.OPEN_HALF_LIFE_DAYS
            )
    return boosts


async def _candidates(
    conn: aiosqlite.Connection,
    settings: Settings,
    user_id: int,
    now: datetime,
    history: _Device,
) -> list[selection.Candidate]:
    opened = set(
        await _column(
            conn,
            "select video_id from opens where user_id = ? and last_open_utc >= ?",
            user_id,
            stamp(now - timedelta(days=OPENED_EXCLUSION_DAYS)),
        )
    )
    followed = set(
        await _column(
            conn,
            "select channel_id from subscriptions where user_id = ? and last_seen_by_user >= ?",
            user_id,
            stamp(now - timedelta(days=ACTIVE_SUBSCRIPTION_DAYS)),
        )
    )
    shown = set(
        await _column(
            conn,
            "select distinct v.author_id from impressions i join videos v"
            " on v.video_id = i.video_id where i.user_id = ? and i.ts_utc >= ?",
            user_id,
            stamp_us(now - timedelta(hours=NOVELTY_HOURS)),
        )
    )
    boosts = await _open_boosts(conn, user_id, now)

    rows: dict[str, tuple[aiosqlite.Row, str]] = {}
    async with conn.execute(
        f"select {_VIDEO_COLUMNS}, p.source, e.last_served, e.times_carried"
        " from pool p join videos v on v.video_id = p.video_id"
        " left join exposures e on e.user_id = p.user_id and e.video_id = p.video_id"
        " where p.user_id = ? and p.expires_at > ?",
        (user_id, stamp(now)),
    ) as cur:
        for row in await cur.fetchall():
            rows[row["video_id"]] = (row, row["source"])
    # The previous list's items can carry over even after they left the pool.
    missing = [v for v in (history.last_positions or {}) if v not in rows]
    for start in range(0, len(missing), 500):
        part = missing[start : start + 500]
        marks = ",".join("?" * len(part))
        async with conn.execute(
            f"select {_VIDEO_COLUMNS}, e.last_served, e.times_carried from videos v"
            " left join exposures e on e.user_id = ? and e.video_id = v.video_id"
            f" where v.video_id in ({marks})",
            (user_id, *part),
        ) as cur:
            for row in await cur.fetchall():
                rows[row["video_id"]] = (row, "other")

    out = []
    positions = history.last_positions or {}
    for video_id, (row, source) in rows.items():
        video = _video(row)
        if video_id in opened or not is_servable(video, settings.shorts_max_seconds):
            continue
        # Only the ledger decides what is new: `last_served` is set for every served video.
        out.append(
            selection.Candidate(
                video=video,
                source=source,
                followed=video.author_id in followed,
                open_boost=boosts.get(video.author_id, 0.0),
                channel_shown_24h=video.author_id in shown,
                served=row["last_served"] is not None,
                last_served=row["last_served"],
                carried_before=bool(row["times_carried"]),
                prev_position=positions.get(video_id),
                in_prev_prev=video_id in history.before_last_ids,
            )
        )
    return out


async def never_served_eligible(db: Database, settings: Settings, name: str, now: datetime) -> int:
    """Pool items the user could still be served as new, for /status."""
    async with db.reading() as conn:
        rows = await _column(conn, "select id from users where name = ?", name)
        if not rows:
            return 0
        candidates = await _candidates(conn, settings, rows[0], now, _Device())
    return sum(1 for c in candidates if not c.served)


async def serve_list(
    db: Database, settings: Settings, name: str, device: str, now: datetime
) -> Served:
    """The list for this request. Raises, and records nothing, if any item fails validation."""
    async with db.immediate() as conn:
        user_id = await _user_id(conn, name, stamp(now))
        history = await _device_history(conn, user_id, device)

        window = timedelta(seconds=settings.repeat_window_s)
        if history.last_ts is not None and now - history.last_ts < window:
            videos = await _repeat_list(conn, user_id, history)
            items = [to_list_item(v) for v in videos]
            for item in items:
                validate_list_item(item)
            return Served(items, Counter(), repeat=True)

        candidates = await _candidates(conn, settings, user_id, now, history)
        seed = _seed(name, now)
        picks = selection.build(
            candidates,
            selection.Params(
                settings.list_size,
                min(settings.head_size, settings.list_size),
                settings.carry_max,
                now.astimezone(settings.tz),
            ),
            random.Random(seed),
        )
        items = [to_list_item(p.candidate.video) for p in picks]
        for item in items:
            validate_list_item(item)
        if not picks:
            # An empty list is not recorded: it would break the carry-over chain and open a
            # repeat window over a pool that may fill a moment later.
            return Served([], Counter(), repeat=False)

        ts = stamp_us(now)
        cur = await conn.execute(
            "insert into lists (user_id, device, ts_utc, seed, algorithm_version, size)"
            " values (?, ?, ?, ?, ?, ?)",
            (user_id, device, ts, f"{seed:016x}", selection.ALGORITHM_VERSION, len(picks)),
        )
        list_id = cur.lastrowid
        await conn.executemany(
            "insert into impressions (list_id, user_id, video_id, ts_utc, device, position, kind)"
            " values (?, ?, ?, ?, ?, ?, ?)",
            [
                (list_id, user_id, p.candidate.video_id, ts, device, p.position, p.kind)
                for p in picks
            ],
        )
        await conn.executemany(
            "insert into exposures (user_id, video_id, first_served, last_served, times_served,"
            " times_carried) values (?, ?, ?, ?, 1, ?)"
            " on conflict (user_id, video_id) do update set last_served = excluded.last_served,"
            " times_served = times_served + 1,"
            " times_carried = times_carried + excluded.times_carried",
            [
                (user_id, p.candidate.video_id, ts, ts, int(p.kind == selection.CARRIED))
                for p in picks
            ],
        )
        return Served(items, Counter(p.kind for p in picks), repeat=False)

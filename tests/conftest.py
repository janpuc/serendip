import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import respx

from serendip import consumer, migrate
from serendip.auth import Authenticator
from serendip.config import Settings
from serendip.db import Database
from serendip.feed import FeedJob
from serendip.followups import Followups
from serendip.state import State
from serendip.upstream import ServerClient

ROOT = Path(__file__).parent.parent
UA = "Mozilla/5.0 (Macintosh) Safari/605.1.15 serendip-tv"
YATTEE = "http://yattee.test"
START = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)


def basic(user: str = "jan", password: str = "pw") -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def sha256_hex(header: str) -> str:
    return hashlib.sha256(header.encode()).hexdigest()


def channel_id(n: int) -> str:
    return f"UC{n:022d}"


def video_id(n: int) -> str:
    return f"v{n:010d}"


class FakeClock:
    """sleep() moves the clock instead of waiting. The monotonic clock moves with the wall clock
    except in jump_wall()."""

    def __init__(self, start: datetime = START):
        self._now = start
        self._monotonic = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds=seconds)
        await asyncio.sleep(0)

    def advance(self, **kwargs) -> None:
        delta = timedelta(**kwargs)
        self._now += delta
        self._monotonic += delta.total_seconds()

    def jump_wall(self, **kwargs) -> None:
        """The wall clock is set forward (or back, if negative) and time did not pass."""
        self._now += timedelta(**kwargs)


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "db_path": str(tmp_path / "serendip.db"),
        "device_ua_map": "serendip-tv=tv,serendip-phone=phone",
        # The default test credential belongs to the one configured user.
        "users": f"jan={sha256_hex(basic())}",
    }
    return Settings(_env_file=None, **{**values, **overrides})


async def make_state(tmp_path, clock=None, **overrides) -> State:
    settings = make_settings(tmp_path, **overrides)
    migrate.migrate(settings.db_path)
    db = await Database.connect(settings.db_path)
    auth = Authenticator(settings)
    kwargs = {"clock": clock} if clock else {}
    return State(settings, db, auth, asyncio.Queue(maxsize=settings.ingest_queue_max), **kwargs)


async def make_jobs_state(tmp_path, clock=None, **overrides) -> State:
    """A state with the service user set, and the feed job and follow-ups wired to it."""
    overrides = {
        "yattee_url": YATTEE,
        "yattee_username": "serendip",
        "yattee_password": "s3cret-password",
        **overrides,
    }
    state = await make_state(tmp_path, clock or FakeClock(), **overrides)
    client = ServerClient(state.settings, state.breaker, state.clock)
    state.client = client  # kept for closing in tests
    state.feed = FeedJob(state.settings, state.db, state.breaker, state.clock, client)
    if state.settings.related_source == "followup":
        state.followups = Followups(state.settings, state.db, state.breaker, state.clock, client)
    return state


async def drain(state: State) -> None:
    """Runs the consumer until the queue is empty."""
    task = asyncio.create_task(consumer.run(state))
    try:
        await asyncio.wait_for(state.queue.join(), 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
async def state(tmp_path):
    st = await make_state(tmp_path)
    yield st
    await st.db.close()


@pytest.fixture
def server():
    """A fake Yattee Server; any call it has no route for fails the test."""
    with respx.mock(base_url=YATTEE, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def make_jobs(tmp_path, server):
    """A factory for states with the service user set; everything it made is closed after."""
    made = []

    async def factory(**overrides) -> State:
        st = await make_jobs_state(tmp_path, **overrides)
        made.append(st)
        return st

    yield factory
    for st in made:
        await st.client.close()
        await st.db.close()


@pytest.fixture
async def jobs_state(make_jobs):
    return await make_jobs()


def server_item(n: int, *, channel: int | None = None, length: int = 600, **extra) -> dict:
    """A video as Yattee Server returns it in the feed and in recommendedVideos."""
    item = {
        "type": "video",
        "videoId": video_id(n),
        "title": f"Video {n}",
        "author": f"Channel {channel if channel is not None else n}",
        "authorId": channel_id(channel if channel is not None else n),
        "lengthSeconds": length,
        "published": int((START - timedelta(days=2)).timestamp()),
        "publishedText": "2 days ago",
        "viewCount": 1000 + n,
        "videoThumbnails": [{"quality": "x", "url": f"{YATTEE}/thumb/{n}.jpg"}],
        "liveNow": False,
        "isUpcoming": False,
    }
    return {**item, **extra}


async def seed_subs(state, items: list[dict], name: str = "jan") -> None:
    """Puts feed items in the user's `subs` pool and the user's channels in subscriptions."""
    from serendip.catalog import parse_item
    from serendip.db import stamp

    now = state.clock.now()
    records = [r for r in (parse_item(i, now) for i in items) if r]
    await state.db.record_subscriptions(
        name, "tv", stamp(now), [(r.author_id, None) for r in records]
    )
    await state.db.store_feed(name, now, records)


async def seed_related(
    state, items: list[dict], name: str = "jan", seed: str = "seed0000000"
) -> None:
    from serendip.catalog import parse_item

    now = state.clock.now()
    records = [r for r in (parse_item(i, now) for i in items) if r]
    await state.db.store_related([name], now, seed, None, records)


async def rows(state, sql, *args):
    async with state.db._conn.execute(sql, args) as cur:
        return [tuple(r) for r in await cur.fetchall()]


def video_response(n: int, related: list[dict] | None = None, **extra) -> dict:
    """GET /api/v1/videos/{id}: the full response, with the parts serendip does not read."""
    body = {
        **server_item(n),
        "description": "d",
        "formatStreams": [{"url": f"{YATTEE}/proxy/relay/abc?sig=SECRET"}],
        "adaptiveFormats": [],
        "extractionMethod": "hybrid",
    }
    if related is not None:
        body["recommendedVideos"] = related
    return {**body, **extra}


def route_video(server, n: int, **kwargs):
    return server.get(f"/api/v1/videos/{video_id(n)}").respond(**kwargs)


async def wait_idle(followups, limit: int = 5000) -> None:
    """Lets the follow-up worker finish what it has."""
    for _ in range(limit):
        if not followups.pending_count:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("follow-up worker did not finish")


class Worker:
    """Runs the follow-up worker for the length of a test."""

    def __init__(self, followups):
        self.followups = followups

    async def __aenter__(self):
        self.task = asyncio.create_task(self.followups.run())
        await self.followups.ready.wait()
        return self

    async def __aexit__(self, *exc):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

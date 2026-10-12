"""The feed job (DESIGN 5, 7): subscription uploads from POST /api/v1/feed.

The post is not free of side effects on the server (it refreshes `last_requested` and queues
fetches of channels it has not cached), so it carries only channels the user posted in the last
30 days, and at most one call goes out every FEED_INTERVAL_S across all users. Each call carries
one chunk of up to 500 channels; a durable cursor (user, chunk offset) takes users and chunks in
turn, one per interval. The call is reserved before it is sent, so a restart or a crash loop
cannot bring the next one forward, and it never goes out while the breaker is open.
"""

from __future__ import annotations

from datetime import timedelta

import structlog

from . import metrics
from .breaker import Breaker
from .catalog import parse_item
from .clock import Clock
from .config import Settings
from .db import ACTIVE_SUBSCRIPTION_DAYS, Database, parse_stamp, stamp_us
from .timing import MonotonicLedger
from .upstream import ServerClient

log = structlog.get_logger()

# The server accepts at most 500 channels per request (routers/subscriptions.py).
CHUNK_SIZE = 500
# How often the loop looks for a call that is due.
POLL_S = 300


class FeedJob:
    def __init__(
        self, settings: Settings, db: Database, breaker: Breaker, clock: Clock, client: ServerClient
    ):
        self._settings = settings
        self._db = db
        self._breaker = breaker
        self._clock = clock
        self._client = client
        self._ledger = MonotonicLedger(clock, timedelta(seconds=settings.feed_interval_s))

    async def run(self) -> None:
        while True:
            try:
                await self.run_due()
            except Exception as exc:
                log.warning("feed_job_failed", error=type(exc).__name__)
            await self._clock.sleep(min(POLL_S, self._settings.feed_interval_s))

    async def run_due(self) -> str | None:
        """Makes the next feed call if the interval since the last reservation has passed, by
        the stored wall-clock time and by this process's monotonic clock. Returns the outcome,
        or None when nothing is due. A restart within the interval does not post again."""
        outcome = await self._run_due()
        if outcome is not None:
            metrics.jobs.labels("feed", outcome).inc()
        return outcome

    async def _run_due(self) -> str | None:
        now = self._clock.now()
        interval = timedelta(seconds=self._settings.feed_interval_s)
        last = await self._db.last_feed_started()
        if last is not None and now - parse_stamp(last) < interval:
            return None
        in_process = self._ledger.reservations(now)
        if in_process and now - in_process[0] < interval:
            return None
        target = await self._next_target(now)
        if target is None:
            return None
        if await self._breaker.is_open():
            return "paused_breaker"
        return await self._post(now, *target)

    async def _next_target(self, now) -> tuple[str, int, list[str]] | None:
        """(user, chunk offset, that user's channels) at the cursor, or the next one that has
        a chunk to send, wrapping around."""
        since = now - timedelta(days=ACTIVE_SUBSCRIPTION_DAYS)
        names = await self._db.users_with_subscriptions(since)
        cursor_name, cursor_offset = await self._db.feed_cursor() or ("", 0)
        order = [n for n in names if n >= cursor_name] + [n for n in names if n < cursor_name]
        candidates = [(n, cursor_offset if n == cursor_name else 0) for n in order]
        if cursor_name in names:
            candidates.append((cursor_name, 0))  # the cursor's own user again, from the start
        for name, offset in candidates:
            channels = [c for c, _name in await self._db.active_subscriptions(name, since)]
            if offset < len(channels):
                return name, offset, channels
        return None

    async def _post(self, now, name: str, offset: int, channels: list[str]) -> str:
        chunk = channels[offset : offset + CHUNK_SIZE]
        self._ledger.record()
        run_id = await self._db.feed_reserve(
            name, stamp_us(now), offset, len(chunk), name, offset + CHUNK_SIZE
        )
        status = "ready"
        items = 0
        truncated = False
        try:
            reply = await self._client.request(
                "feed",
                "POST",
                "/api/v1/feed",
                json_body={
                    "channels": [{"channel_id": c, "site": "youtube"} for c in chunk],
                    "limit": self._settings.feed_limit,
                    "offset": 0,
                },
                accept=_feed_shape_ok,
            )
            if reply.skipped:
                # The reservation stands: a skipped send still used the interval.
                status = "skipped"
                outcome = (
                    "paused_breaker" if reply.skipped == "breaker" else f"skipped_{reply.skipped}"
                )
            elif reply.trip:
                status = outcome = "tripped"
            else:
                data = reply.data
                assert isinstance(data, dict)
                if data["status"] == "fetching":
                    status = "fetching"
                truncated = data.get("has_more") is True
                parsed = self._clock.now()
                records = [
                    r for r in (parse_item(v, parsed) for v in data["videos"]) if r is not None
                ]
                items = await self._db.store_feed(name, parsed, records)
                outcome = status
        except BaseException:
            await self._db.feed_run_finish(
                run_id, stamp_us(self._clock.now()), "failed", items, truncated
            )
            raise
        await self._db.feed_run_finish(
            run_id, stamp_us(self._clock.now()), status, items, truncated
        )
        return outcome


def _feed_shape_ok(data: object) -> bool:
    return (
        isinstance(data, dict)
        and data.get("status") in ("ready", "fetching")
        and isinstance(data.get("videos"), list)
    )

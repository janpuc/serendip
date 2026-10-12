"""Follow-ups (DESIGN 5.1, 7): ask the server for a video the user just opened.

About 60 s after an observed open serendip calls GET /api/v1/videos/{id} on the in-cluster server
and keeps the video's `recommendedVideos`. The call may be answered from the server's caches or
may be a full extraction against YouTube; serendip cannot tell, so each one is reserved in the
budget as a full extraction. A follow-up is never delayed past its deadline and never retried.
The breaker and the deadline are checked again by the client's gate at the moment of sending, after
the reservation is written; a reservation whose send was skipped still counts in the budget.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog

from . import metrics
from .breaker import Breaker
from .catalog import parse_item
from .clock import Clock
from .config import Settings
from .db import Database, parse_stamp, stamp_us
from .events import VIDEO_ID
from .timing import MonotonicLedger
from .upstream import ServerClient

log = structlog.get_logger()

# A video fetched this recently is not asked for again.
REFETCH_AFTER = timedelta(hours=6)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
# More waiting than this cannot start before its deadline anyway; bounds memory under a flood.
MAX_PENDING = 100
MAX_RELATED_KEPT = 50
_METHOD = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


@dataclass
class Pending:
    video_id: str
    users: set[str]
    due_at: datetime
    deadline_at: datetime


@dataclass
class Followups:
    settings: Settings
    db: Database
    breaker: Breaker
    clock: Clock
    client: ServerClient
    _pending: deque[Pending] = field(default_factory=deque, init=False)
    _ids: dict[str, Pending] = field(default_factory=dict, init=False)
    # Admission checks _ids, reads the database and enqueues; one at a time, so two opens of
    # one video cannot both be scheduled.
    _admission: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _ledger: MonotonicLedger = field(init=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    # Set once run() has marked what a crash left unfinished.
    ready: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def __post_init__(self) -> None:
        self._ledger = MonotonicLedger(self.clock, DAY)

    def in_process_reservations(self, now: datetime) -> list[datetime]:
        return self._ledger.reservations(now)

    @property
    def pending_count(self) -> int:
        return len(self._ids)

    async def trigger(self, user: str, video_id: str, received_at: datetime) -> str:
        """Called for an open recorded by this process. Returns what became of it."""
        outcome = await self._admit(user, video_id, received_at)
        if outcome != "scheduled":
            metrics.jobs.labels("followup", outcome).inc()
        return outcome

    async def _admit(self, user: str, video_id: str, received_at: datetime) -> str:
        if not VIDEO_ID.match(video_id):
            return "skipped_invalid"
        async with self._admission:
            return await self._admit_locked(user, video_id, received_at)

    async def _admit_locked(self, user: str, video_id: str, received_at: datetime) -> str:
        known = self._ids.get(video_id)
        if known is not None:
            known.users.add(user)
            return "skipped_duplicate"
        seen = await self.db.followup_seen(video_id)
        if seen.failed_before:
            return "skipped_failed"
        if seen.reserved_last and received_at - parse_stamp(seen.reserved_last) < REFETCH_AFTER:
            return "skipped_recent"
        if await self.db.is_known_live(video_id):
            return "skipped_live"
        if len(self._pending) >= MAX_PENDING:
            return "dropped_queue_full"
        job = Pending(
            video_id,
            {user},
            received_at + timedelta(seconds=self.settings.followup_delay_s),
            received_at + timedelta(seconds=self.settings.followup_deadline_s),
        )
        self._pending.append(job)
        self._ids[video_id] = job
        self._wake.set()
        return "scheduled"

    async def run(self) -> None:
        """One follow-up at a time. Pending work from before a restart is not replayed."""
        interrupted = await self.db.interrupt_followups(stamp_us(self.clock.now()))
        if interrupted:
            log.warning("followups_interrupted", count=interrupted)
        self.ready.set()
        while True:
            while not self._pending:
                self._wake.clear()
                await self._wake.wait()
            job = self._pending.popleft()
            try:
                outcome = await self._handle(job)
            except Exception as exc:
                outcome = "error"
                log.warning("followup_failed", error=type(exc).__name__)
            finally:
                self._ids.pop(job.video_id, None)
            metrics.jobs.labels("followup", outcome).inc()

    async def _handle(self, job: Pending) -> str:
        while True:
            now = self.clock.now()
            if now > job.deadline_at:
                return "dropped_deadline"
            if await self.breaker.is_open():
                return "dropped_breaker"
            start_at, blocker = await self._earliest_start(job.due_at, now)
            if start_at > job.deadline_at:
                return f"dropped_{blocker}"
            if start_at > now:
                await self.clock.sleep((start_at - now).total_seconds())
                continue
            return await self._fetch(job, now)

    async def _earliest_start(self, due_at: datetime, now: datetime) -> tuple[datetime, str]:
        """When the budget lets a follow-up start, and what is holding it back.

        Rolling windows over the reservations, spacing from the newest one, counted twice: by the
        stored wall-clock times and by this process's monotonic clock. The later start wins, so
        a jump of the wall clock in either direction cannot release anything. Jitter, if any is
        added later, must only delay.
        """
        reserved = [
            parse_stamp(t) for t in await self.db.followup_reservations_since(stamp_us(now - DAY))
        ]
        by_wall = start_after(due_at, reserved, self.settings)
        by_monotonic = start_after(due_at, self._ledger.reservations(now), self.settings)
        return max(by_wall, by_monotonic, key=lambda found: found[0])

    async def _fetch(self, job: Pending, now: datetime) -> str:
        self._ledger.record()
        followup_id = await self.db.followup_reserve(job.video_id, stamp_us(now))
        await self.db.followup_mark_sent(followup_id, stamp_us(self.clock.now()))
        # The awaits above can outlast the deadline or see the breaker trip; the gate checks both
        # again at the moment of sending.
        reply = await self.client.request(
            "video",
            "GET",
            f"/api/v1/videos/{job.video_id}",
            deadline=job.deadline_at,
            accept=lambda data: _video_shape_ok(data, job.video_id),
        )
        finished = stamp_us(self.clock.now())

        async def finish(status: str, outcome: str | None, method: str | None = None, count=None):
            await self.db.followup_finish(
                followup_id,
                finished,
                status,
                seconds=None if reply.skipped else round(reply.seconds, 3),
                extraction_method=method,
                outcome=outcome,
                related_count=count,
            )

        if reply.skipped:
            await finish("skipped", reply.skipped)
            return f"dropped_{reply.skipped}"
        metrics.followup_seconds.observe(reply.seconds)
        if reply.trip:
            await finish("tripped", reply.trip[1])
            return "tripped"
        if reply.not_found:
            await finish("not_found", "HTTP 404")
            return "not_found"

        data = reply.data
        assert isinstance(data, dict)
        related_raw = data.get("recommendedVideos")

        method = data.get("extractionMethod")
        method = method if isinstance(method, str) and _METHOD.match(method) else None
        parsed = self.clock.now()
        seed = parse_item(data, parsed)
        related = [
            r
            for r in (parse_item(item, parsed) for item in (related_raw or [])[:MAX_RELATED_KEPT])
            if r
        ]
        await self.db.store_related(sorted(job.users), parsed, job.video_id, seed, related)
        await finish("ok", None, method, len(related))
        return "ok"


def start_after(
    due_at: datetime, reserved: list[datetime], settings: Settings
) -> tuple[datetime, str]:
    """The earliest start the rolling windows and the spacing allow, given reservation times
    (newest first), and what holds it back."""
    start = due_at
    blocker = "spacing"
    if reserved:
        start = max(start, reserved[0] + timedelta(seconds=settings.followup_min_spacing_s))
    for window, limit in (
        (HOUR, settings.followup_per_hour),
        (DAY, settings.followup_per_day),
    ):
        if limit == 0:
            return datetime.max.replace(tzinfo=start.tzinfo), "budget"
        while True:
            inside = [t for t in reserved if t > start - window]
            if len(inside) < limit:
                break
            # The limit-th newest reservation leaves the window an hour (a day) after it.
            start = max(start, inside[limit - 1] + window)
            blocker = "budget"
    return start, blocker


def _video_shape_ok(data: object, video_id: str) -> bool:
    if not isinstance(data, dict) or data.get("videoId") != video_id:
        return False
    related = data.get("recommendedVideos")
    return related is None or isinstance(related, list)


async def budget_left(
    db: Database,
    settings: Settings,
    now: datetime,
    in_process: list[datetime] | None = None,
) -> dict[str, int | str | None]:
    """What the rolling budgets allow right now, and when the spacing ends: the smaller of what
    the stored times say and what this process's monotonic record says (`in_process`)."""
    stored = [parse_stamp(t) for t in await db.followup_reservations_since(stamp_us(now - DAY))]
    hour = day = None
    spacing_ends = None
    for reserved in (stored, in_process or []):
        in_hour = sum(1 for t in reserved if t > now - HOUR)
        left_hour = max(settings.followup_per_hour - in_hour, 0)
        left_day = max(settings.followup_per_day - len(reserved), 0)
        hour = left_hour if hour is None else min(hour, left_hour)
        day = left_day if day is None else min(day, left_day)
        if reserved:
            ends = reserved[0] + timedelta(seconds=settings.followup_min_spacing_s)
            if ends > now and (spacing_ends is None or ends > spacing_ends):
                spacing_ends = ends
    return {
        "hour": hour,
        "day": day,
        "spacing_ends_at": stamp_us(spacing_ends) if spacing_ends else None,
    }

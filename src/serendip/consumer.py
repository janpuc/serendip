"""Drains the ingest queue: validates the user, extracts the event, writes it."""

from __future__ import annotations

import asyncio

import structlog

from . import metrics
from .auth import MALFORMED, UNKNOWN_CREDENTIAL
from .db import stamp
from .events import OPEN, SEARCH, SUBSCRIPTIONS_SEEN, IngestItem, ParsedEvent, parse
from .state import State

log = structlog.get_logger()

# A few tasks keep one slow database write from blocking the rest.
WORKERS = 4


async def run(state: State) -> None:
    async with asyncio.TaskGroup() as group:
        for _ in range(WORKERS):
            group.create_task(_worker(state))


async def _worker(state: State) -> None:
    while True:
        item = await state.queue.get()
        try:
            await process(state, item)
        except Exception as exc:
            # The type only: exception text can carry request data.
            state.drop("error")
            log.warning("event_failed", kind=item.kind, error=type(exc).__name__)
        finally:
            state.queue.task_done()


async def process(state: State, item: IngestItem) -> None:
    event = parse(item)
    if isinstance(event, str):
        state.drop(event)
        return

    verdict = state.auth.check(item.authorization)
    if verdict.name is None:
        state.drop("malformed_credential" if verdict.result == MALFORMED else UNKNOWN_CREDENTIAL)
        return

    ts = stamp(item.received_at)
    device = state.settings.device_for(item.user_agent)
    await _store(state, event, verdict.name, device, ts)
    # Events finish out of order; /status must not move backwards.
    if state.last_event_at is None or ts > state.last_event_at:
        state.last_event_at = ts
    metrics.events.labels(event.kind).inc()
    # Counts only: video ids, queries and channel names are user data.
    log.debug("event_stored", kind=event.kind, user=verdict.name, device=device)


async def _store(state: State, event: ParsedEvent, name: str, device: str, ts: str) -> None:
    db = state.db
    if event.kind == OPEN:
        assert event.video_id is not None
        await db.record_open(name, device, ts, event.video_id, event.query)
    elif event.kind == SEARCH:
        assert event.query is not None
        await db.record_search(name, device, ts, event.query)
    elif event.kind == SUBSCRIPTIONS_SEEN:
        await db.record_subscriptions(name, device, ts, event.channels)

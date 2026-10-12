"""The persistent breaker (DESIGN 7): the one switch that stops every call to Yattee Server.

The state lives in the database, not in memory, so it survives a restart and so that
`python -m serendip breaker reset` run in another process takes effect without restarting the
server. It is checked before every call.
"""

from __future__ import annotations

import structlog

from . import metrics
from .clock import Clock, SystemClock
from .db import BreakerState, Database, stamp
from .logs import scrub_urls

log = structlog.get_logger()

BOT_CHECK = "bot_check"
OPERATIONAL_STOP = "operational_stop"  # 401, 403 or 429 from the server
ERROR = "error"  # 5xx, malformed response, transport error, timeout
MANUAL = "manual"

MAX_REASON_CHARS = 200
INITIAL_REASON = "initial: confirm before enabling jobs"


class Breaker:
    def __init__(self, db: Database, clock: Clock | None = None):
        self._db = db
        self._clock = clock or SystemClock()

    async def state(self) -> BreakerState:
        state = await self._db.breaker_state()
        metrics.breaker_open.set(int(state.open))
        return state

    async def is_open(self) -> bool:
        return (await self.state()).open

    async def trip(self, kind: str, reason: str) -> bool:
        """Opens the breaker; True if this call did. `reason` is a short code of ours, never
        response text."""
        reason = scrub_urls(reason.strip())[:MAX_REASON_CHARS]
        tripped = await self._db.trip_breaker(kind, reason, stamp(self._clock.now()))
        metrics.breaker_open.set(1)
        if tripped:
            log.error("breaker_tripped", kind=kind, reason=reason)
        return tripped

    async def initialise(self, start_open: bool) -> bool:
        """Called at every start. Only the first start with a fresh breaker row has an effect,
        and it opens the breaker if `start_open`. True if it did."""
        opened = await self._db.initialise_breaker(
            start_open, MANUAL, INITIAL_REASON, stamp(self._clock.now())
        )
        if opened:
            metrics.breaker_open.set(1)
            log.error("breaker_tripped", kind=MANUAL, reason=INITIAL_REASON)
        return opened

    async def reset(self) -> bool:
        reset = await self._db.reset_breaker()
        metrics.breaker_open.set(0)
        if reset:
            log.warning("breaker_reset")
        return reset

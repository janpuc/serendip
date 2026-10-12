"""What the ingest app, the consumer and the API app share. One process, one event loop."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import metrics
from .auth import Authenticator
from .breaker import Breaker
from .clock import Clock, SystemClock
from .config import Settings
from .db import Database
from .events import IngestItem

if TYPE_CHECKING:
    from .feed import FeedJob
    from .followups import Followups


@dataclass
class State:
    settings: Settings
    db: Database
    auth: Authenticator
    queue: asyncio.Queue[IngestItem]
    dropped: Counter[str] = field(default_factory=Counter)
    last_event_at: str | None = None
    # False once a critical background task has died; /health then answers 503.
    healthy: bool = True
    stopping: bool = False
    clock: Clock = field(default_factory=SystemClock)
    breaker: Breaker = field(init=False)
    # None when the service user is not configured, or when RELATED_SOURCE is off.
    followups: Followups | None = None
    feed: FeedJob | None = None

    def __post_init__(self) -> None:
        self.breaker = Breaker(self.db, self.clock)

    def drop(self, reason: str) -> None:
        self.dropped[reason] += 1
        metrics.ingest_dropped.labels(reason).inc()

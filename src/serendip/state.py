"""What the ingest app, the consumer and the API app share. One process, one event loop."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field

from . import metrics
from .auth import Authenticator
from .config import Settings
from .db import Database
from .events import IngestItem


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

    def drop(self, reason: str) -> None:
        self.dropped[reason] += 1
        metrics.ingest_dropped.labels(reason).inc()

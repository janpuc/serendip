"""Budgets and the process's own monotonic record of its sends.

The database holds wall-clock reservation times. A clock that jumps forward would release a budget
early, and one that jumps back would hold it too long or too short; the monotonic clock cannot
jump. So within one process every reservation is also noted by monotonic time, and a budget
applies the later of the two answers.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .clock import Clock


class MonotonicLedger:
    """When this process made its reservations, by the monotonic clock. Nothing from before the
    process started; the database covers that."""

    def __init__(self, clock: Clock, horizon: timedelta):
        self._clock = clock
        self._horizon_s = horizon.total_seconds()
        self._marks: list[float] = []

    def record(self) -> None:
        self._marks.append(self._clock.monotonic())

    def reservations(self, now: datetime) -> list[datetime]:
        """The reservations as they would read on the wall clock if it had advanced as the
        monotonic clock did, newest first."""
        mono = self._clock.monotonic()
        self._marks = [m for m in self._marks if mono - m <= self._horizon_s]
        return [now - timedelta(seconds=mono - m) for m in reversed(self._marks)]

"""Choosing a list (DESIGN 6, 6.1). Pure: candidates and a random generator in, a list out.

lists.py reads the candidates from SQLite and writes the result in the same transaction.
"""

from __future__ import annotations

import math
import random
from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from .catalog import StoredVideo
from .db import parse_stamp

ALGORITHM_VERSION = 1

NEW = "new"
CARRIED = "carried"
RESHOWN = "reshown"

SUBS = "subs"

FOLLOWED_BASE = 2.0
OTHER_BASE = 1.0
OPEN_HALF_LIFE_DAYS = 30.0
FRESHNESS_FLOOR = 0.3
FRESHNESS_DAYS = 14.0
NOVELTY_PENALTY = 0.6
GEM_BONUS = 1.3
GEM_MIN_AGE_DAYS = 365.0
GEM_MIN_VIEWS = 500_000
SUBS_SHARE = 0.4
CAP_FOLLOWED = 2
CAP_OTHER = 1
LONG_VIDEO_SECONDS = 15 * 60

# (under 15 minutes, 15 minutes or more); modest, the same for every device (DESIGN 6 step 2).
LENGTH_PRIOR = {
    "morning": (1.3, 0.7),
    "afternoon": (1.0, 1.0),
    "evening": (0.7, 1.3),
    "night": (1.1, 0.9),
}


def daypart(hour: int) -> str:
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "night"


@dataclass(frozen=True)
class Candidate:
    video: StoredVideo
    source: str  # subs, related, adjacent, or 'other' for a carried video that left the pool
    followed: bool  # the channel is one of the user's active subscriptions
    open_boost: float  # recency-weighted opens on the channel
    channel_shown_24h: bool
    served: bool  # in the exposure ledger
    last_served: str | None
    carried_before: bool
    prev_position: int | None  # in the previous list on this device
    in_prev_prev: bool  # in the list before that too

    @property
    def video_id(self) -> str:
        return self.video.video_id

    @property
    def channel(self) -> str:
        return self.video.author_id


@dataclass(frozen=True)
class Params:
    list_size: int
    head_size: int
    carry_max: float
    now: datetime  # local time in the configured zone, for the daypart


@dataclass(frozen=True)
class Pick:
    candidate: Candidate
    kind: str
    position: int  # 1-based


def weight(c: Candidate, now: datetime) -> float:
    """All factors positive (DESIGN 6 step 2)."""
    interest = (FOLLOWED_BASE if c.followed else OTHER_BASE) + c.open_boost
    published = parse_stamp(c.video.published_est or c.video.found_at)
    age_days = max((now - published).total_seconds() / 86400, 0.0)
    freshness = 1.0
    if c.source == SUBS:
        freshness = FRESHNESS_FLOOR + (1 - FRESHNESS_FLOOR) * math.exp(-age_days / FRESHNESS_DAYS)
    gem = (
        GEM_BONUS
        if age_days > GEM_MIN_AGE_DAYS and (c.video.view_count or 0) >= GEM_MIN_VIEWS
        else 1.0
    )
    short_prior, long_prior = LENGTH_PRIOR[daypart(now.hour)]
    length = long_prior if c.video.length_s >= LONG_VIDEO_SECONDS else short_prior
    novelty = NOVELTY_PENALTY if c.channel_shown_24h else 1.0
    return interest * freshness * gem * length * novelty


def race(candidates: Sequence[Candidate], now: datetime, rng: random.Random) -> list[Candidate]:
    """Weighted sampling without replacement as an exponential race: key = -ln(U) / w, smallest
    first. Candidates are keyed in video id order, so the result depends only on the snapshot
    and the seed."""
    keyed = []
    for c in sorted(candidates, key=lambda c: c.video_id):
        u = 1.0 - rng.random()  # (0, 1], so the log is finite
        keyed.append((-math.log(u) / weight(c, now), c.video_id, c))
    keyed.sort(key=lambda k: (k[0], k[1]))
    return [c for _key, _id, c in keyed]


class _Caps:
    """At most 2 items per followed channel and 1 per other channel, over the whole list."""

    def __init__(self) -> None:
        self._count: Counter[str] = Counter()

    def allows(self, c: Candidate) -> bool:
        return self._count[c.channel] < (CAP_FOLLOWED if c.followed else CAP_OTHER)

    def add(self, c: Candidate) -> None:
        self._count[c.channel] += 1

    def remove(self, c: Candidate) -> None:
        self._count[c.channel] -= 1

    def copy(self) -> _Caps:
        other = _Caps()
        other._count = Counter(self._count)
        return other


class _Drawer:
    """Takes candidates in race order while the caps hold. With quotas, a candidate whose side
    (subscriptions or discoveries) is full is set aside, and used only when the order runs out
    first: the mix is a soft target that never costs a slot."""

    def __init__(
        self, ordered: Sequence[Candidate], caps: _Caps, quotas: dict[bool, int] | None = None
    ):
        self._ordered = deque(ordered)
        self._deferred: list[Candidate] = []
        self._caps = caps
        self._quotas = quotas
        self._taken: Counter[bool] = Counter()

    def take(self, k: int) -> list[Candidate]:
        out: list[Candidate] = []
        while len(out) < k and self._ordered:
            c = self._ordered.popleft()
            if not self._caps.allows(c):
                continue
            side = c.source == SUBS
            if self._quotas is not None and self._taken[side] >= self._quotas[side]:
                self._deferred.append(c)
                continue
            self._accept(c, out)
        while len(out) < k and self._deferred:
            c = self._deferred.pop(0)
            if self._caps.allows(c):
                self._accept(c, out)
        return out

    def _accept(self, c: Candidate, out: list[Candidate]) -> None:
        self._caps.add(c)
        self._taken[c.source == SUBS] += 1
        out.append(c)


def _interleave(items: Sequence[Candidate]) -> list[Candidate]:
    """Subscriptions and discoveries evenly spread, each side in its given order."""
    a = [c for c in items if c.source == SUBS]
    b = [c for c in items if c.source != SUBS]
    out: list[Candidate] = []
    i = j = 0
    while i < len(a) or j < len(b):
        if j >= len(b) or (i < len(a) and (i + 0.5) / len(a) <= (j + 0.5) / len(b)):
            out.append(a[i])
            i += 1
        else:
            out.append(b[j])
            j += 1
    return out


def _can_carry(c: Candidate) -> bool:
    return c.prev_position is not None and not c.carried_before and not c.in_prev_prev


def build(candidates: Sequence[Candidate], params: Params, rng: random.Random) -> list[Pick]:
    """The list: head, carry-over, new items, then re-shown items only if the others run out.

    `candidates` are already servable (valid, not opened in 90 days, no Shorts, ...).
    """
    n = params.list_size
    new = race([c for c in candidates if not c.served], params.now, rng)
    carry_pool = race([c for c in candidates if _can_carry(c)], params.now, rng)
    reshown_pool = sorted(
        (c for c in candidates if c.served and c.prev_position is None),
        key=lambda c: (c.last_served or "", c.video_id),
    )

    caps = _Caps()
    c_plan = min(math.floor(params.carry_max * n), len(carry_pool))
    new_slots = n - c_plan
    subs_quota = round(SUBS_SHARE * new_slots)
    new_drawer = _Drawer(new, caps, {True: subs_quota, False: new_slots - subs_quota})

    head = new_drawer.take(min(params.head_size, new_slots))
    carry = _Drawer(carry_pool, caps).take(c_plan)
    chosen_new = head + new_drawer.take(new_slots - len(head))
    chosen_new += new_drawer.take(n - len(chosen_new) - len(carry))

    # Re-shown items fill the shortfall. The carry ceiling is a share of the returned list, so it
    # is checked against the final size, and the last-drawn carried items go first if it fails.
    while True:
        trial = caps.copy()
        reshown = _Drawer(reshown_pool, trial).take(n - len(chosen_new) - len(carry))
        size = len(chosen_new) + len(carry) + len(reshown)
        if len(carry) <= math.floor(params.carry_max * size):
            break
        caps.remove(carry.pop())

    head_len = min(params.head_size, len(chosen_new))
    ordered_new = _interleave(chosen_new)
    body = _interleave(ordered_new[head_len:] + reshown)
    placed = _place_carried(ordered_new[:head_len], body, carry, rng)

    if not placed and candidates:
        # Last resort, rather than an empty page: everything servable is in the previous list or
        # already carried. Show it again, as re-shown items, in a new order.
        again = _Drawer(race(candidates, params.now, rng), _Caps()).take(n)
        placed = _new_order(again, rng)

    new_ids = {c.video_id for c in chosen_new}
    carried_ids = {c.video_id for c in carry}
    picks = []
    for position, c in enumerate(placed, start=1):
        kind = NEW if c.video_id in new_ids else CARRIED if c.video_id in carried_ids else RESHOWN
        picks.append(Pick(c, kind, position))
    return picks


def _new_order(items: list[Candidate], rng: random.Random) -> list[Candidate]:
    """A shuffle that keeps no item at its previous position, when one exists among a few
    tries; a one-item list keeps its only position."""
    order = list(items)
    for _ in range(100):
        rng.shuffle(order)
        if all(c.prev_position != i for i, c in enumerate(order, start=1)):
            break
    return order


def _place_carried(
    head: list[Candidate], body: list[Candidate], carry: list[Candidate], rng: random.Random
) -> list[Candidate]:
    """Head first, carried items at random body positions other than their previous one."""
    carry = list(carry)
    while True:
        total = len(head) + len(body) + len(carry)
        slots = list(range(len(head), total))  # 0-based positions after the head
        for _ in range(100 if carry else 1):
            chosen = rng.sample(slots, len(carry)) if carry else []
            if all(slot + 1 != c.prev_position for slot, c in zip(chosen, carry, strict=True)):
                result: list[Candidate | None] = [None] * total
                result[: len(head)] = head
                for slot, c in zip(chosen, carry, strict=True):
                    result[slot] = c
                rest = iter(body)
                return [c if c is not None else next(rest) for c in result]
        # No arrangement avoids every previous position (a very short list): drop the last one.
        carry.pop()

"""A /popular selection over a realistic pool must stay fast: it runs inside the write lock, so
every other write waits for it (DESIGN 6.1)."""

import asyncio
import time
from datetime import timedelta

import pytest
from conftest import FakeClock, make_state, seed_related, seed_subs, server_item, video_id
from test_popular import PHONE, TV, popular

from serendip import lists
from serendip.db import stamp

POOL = 5000
CHANNELS = 600
OPENS = 200
HISTORY_LISTS = 30
SELECTION_LIMIT_S = 0.250

pytestmark = pytest.mark.benchmark


@pytest.fixture
async def big(tmp_path):
    """5000 candidates on a temp-file database, 200 opens and 30 lists already served."""
    state = await make_state(tmp_path, clock=FakeClock())
    subs = [server_item(i, channel=i % CHANNELS) for i in range(POOL // 2)]
    other = [server_item(POOL // 2 + i, channel=CHANNELS + i % CHANNELS) for i in range(POOL // 2)]
    await seed_subs(state, subs)
    await seed_related(state, other)
    now = state.clock.now()
    for i in range(OPENS):
        ts = stamp(now - timedelta(days=1 + i % 80, minutes=i))
        await state.db.record_open("jan", "tv", ts, video_id(i * 11 % POOL), None)
    for _ in range(HISTORY_LISTS):
        state.clock.advance(minutes=45)
        await lists.serve_list(state.db, state.settings, "jan", "tv", state.clock.now())
    state.clock.advance(minutes=45)
    yield state
    await state.db.close()


async def timed(state, ua: str) -> float:
    started = time.perf_counter()
    assert len(await popular(state, ua)) == 30
    return time.perf_counter() - started


async def test_selection_over_5000_candidates_is_under_250_ms(big):
    elapsed = await timed(big, TV)
    print(f"\n/popular over {POOL} candidates: {elapsed * 1000:.1f} ms")
    assert elapsed < SELECTION_LIMIT_S


async def test_two_simultaneous_requests_wait_on_the_write_lock(big):
    started = time.perf_counter()

    async def request(ua: str) -> float:
        assert len(await popular(big, ua)) == 30
        return time.perf_counter() - started

    first, second = sorted(await asyncio.gather(request(TV), request(PHONE)))
    print(
        f"\ntwo simultaneous /popular: first done at {first * 1000:.1f} ms,"
        f" second at {second * 1000:.1f} ms"
    )
    assert second < 2 * SELECTION_LIMIT_S

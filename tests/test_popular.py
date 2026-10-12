"""GET /api/v1/popular: authentication, response shape, constraints, history, concurrency,
the repeat window (DESIGN 6, 6.1, 11)."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from conftest import (
    UA,
    FakeClock,
    basic,
    channel_id,
    make_state,
    rows,
    seed_related,
    seed_subs,
    server_item,
    video_id,
)

from serendip import lists
from serendip.api import create_api_app
from serendip.db import stamp, stamp_us

PHONE = "Mozilla/5.0 serendip-phone"
TV = UA
AUTH = {"Authorization": basic()}


def client(state) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_api_app(state))
    return httpx.AsyncClient(transport=transport, base_url="http://serendip.test")


async def popular(state, ua: str = TV, **headers) -> list[dict]:
    async with client(state) as c:
        r = await c.get("/api/v1/popular", headers={**AUTH, "User-Agent": ua, **headers})
    assert r.status_code == 200, r.text
    return r.json()


def ids(items) -> list[str]:
    return [i["videoId"] for i in items]


@pytest.fixture
async def st(tmp_path):
    state = await make_state(tmp_path, clock=FakeClock())
    yield state
    await state.db.close()


async def fill(state, subs=40, other=60):
    await seed_subs(state, [server_item(i, channel=i) for i in range(subs)])
    await seed_related(state, [server_item(1000 + i, channel=1000 + i) for i in range(other)])


# --- authentication ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": basic("jan", "wrong")}, {"Authorization": "Bearer abc"},
     {"Authorization": "Basic !!!"}],
)  # fmt: skip
async def test_popular_without_a_known_credential_is_401(st, headers):
    await fill(st)
    async with client(st) as c:
        r = await c.get("/api/v1/popular", headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Basic realm="Yattee Server"'
    assert r.headers["cache-control"] == "private, no-store"
    assert await rows(st, "select count(*) from lists") == [(0,)]


async def test_popular_is_private_no_store(st):
    async with client(st) as c:
        r = await c.get("/api/v1/popular", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == []  # empty pool: 200 and an empty array
    assert r.headers["cache-control"] == "private, no-store"


# --- response shape ---------------------------------------------------------------------------

LIST_ITEM_KEYS = {
    "type", "videoId", "title", "description", "author", "authorId", "authorUrl",
    "lengthSeconds", "published", "publishedText", "viewCount", "viewCountText", "likeCount",
    "videoThumbnails", "liveNow", "isUpcoming", "isShort",
}  # fmt: skip


async def test_items_have_exactly_the_trending_keys_and_standard_thumbnails(st):
    await fill(st, 5, 5)
    items = await popular(st)
    assert len(items) == 10
    for item in items:
        assert set(item) == LIST_ITEM_KEYS
        assert item["type"] == "video"
        for key in ("videoId", "title", "author", "authorId"):
            assert isinstance(item[key], str) and item[key]
        assert isinstance(item["lengthSeconds"], int) and item["lengthSeconds"] > 0
        assert item["liveNow"] is False and item["isUpcoming"] is False
        assert item["authorUrl"] == f"/channel/{item['authorId']}"
        assert [t["quality"] for t in item["videoThumbnails"]] == [
            "default", "medium", "high", "sddefault", "maxres",
        ]  # fmt: skip
        for t in item["videoThumbnails"]:
            assert set(t) == {"quality", "url", "width", "height"}
            assert t["url"].startswith(f"https://i.ytimg.com/vi/{item['videoId']}/")
        # The feed item's own thumbnail URL points at the server host and is never passed on.
        assert "yattee.test" not in str(item)
        assert isinstance(item["published"], int)


async def test_response_decodes_with_the_apps_required_fields(st):
    """YatteeServerAPI.swift:603-646: videoId, title, author, authorId, lengthSeconds."""
    await fill(st, 10, 10)
    for item in await popular(st):
        assert isinstance(item["videoId"], str)
        assert isinstance(item["title"], str)
        assert isinstance(item["author"], str)
        assert isinstance(item["authorId"], str)
        assert type(item["lengthSeconds"]) is int
        assert item["videoThumbnails"] is not None


# --- hard constraints -------------------------------------------------------------------------


async def test_opened_within_90_days_is_excluded_and_older_is_not(st):
    await fill(st, 0, 0)
    now = st.clock.now()
    await seed_related(st, [server_item(i, channel=i) for i in range(1, 4)])
    await st.db.record_open("jan", "tv", stamp(now - timedelta(days=89)), video_id(1), None)
    await st.db.record_open("jan", "tv", stamp(now - timedelta(days=91)), video_id(2), None)
    assert sorted(ids(await popular(st))) == [video_id(2), video_id(3)]


async def test_invalid_shorts_live_upcoming_and_authorless_items_are_dropped(st):
    ok = server_item(1, channel=1)
    await seed_related(
        st,
        [
            ok,
            server_item(2, channel=2, length=180),  # Short by length
            server_item(3, channel=3, length=181),
            server_item(4, channel=4, length=0),
            server_item(5, channel=5, liveNow=True, length=600),
            server_item(6, channel=6, isUpcoming=True, length=600),
            server_item(7, channel=7, isShort=True),
            server_item(8, channel=8, authorId=""),  # refused at parse
            server_item(9, channel=9, title=""),
        ],
    )
    # An authorless row that got into the table some other way is still dropped.
    await st.db._conn.execute(
        "insert into videos values (?, 't', 'a', '', 600, null, null, null, null, 0, 0, null, ?)",
        (video_id(10), stamp(st.clock.now())),
    )
    await st.db._conn.execute(
        "insert into pool values (1, ?, 'related', null, ?, ?)",
        (video_id(10), stamp(st.clock.now()), stamp(st.clock.now() + timedelta(days=1))),
    )
    await st.db._conn.commit()
    assert sorted(ids(await popular(st))) == [video_id(1), video_id(3)]


async def test_channel_caps_in_a_served_list(st):
    await seed_subs(st, [server_item(i, channel=1) for i in range(10)])
    await seed_related(st, [server_item(100 + i, channel=2) for i in range(10)])
    await seed_related(st, [server_item(200 + i, channel=200 + i) for i in range(40)])
    items = await popular(st)
    per_channel = {}
    for i in items:
        per_channel[i["authorId"]] = per_channel.get(i["authorId"], 0) + 1
    assert per_channel[channel_id(1)] == 2  # followed
    assert per_channel[channel_id(2)] == 1  # not followed


async def test_expired_pool_items_are_not_served(st):
    await fill(st, 3, 3)
    await st.db._conn.execute(
        "update pool set expires_at = ?", (stamp(st.clock.now() - timedelta(seconds=1)),)
    )
    await st.db._conn.commit()
    assert await popular(st) == []


# --- refresh behaviour ------------------------------------------------------------------------


async def test_consecutive_refreshes_share_at_most_a_third_and_never_the_head(st):
    await fill(st, 60, 90)
    previous = None
    for _ in range(4):
        items = await popular(st)
        assert len(items) == 30
        if previous is not None:
            assert not set(ids(items[:5])) & set(ids(previous))  # head: never served
            shared = set(ids(items)) & set(ids(previous))
            assert len(shared) <= 9
            for vid in shared:
                assert ids(items).index(vid) != ids(previous).index(vid)
                assert ids(items).index(vid) >= 5
        previous = items
        st.clock.advance(seconds=20)
    # No video in three consecutive lists.
    lists_ = await rows(st, "select id from lists order by id")
    seen = [
        {r[0] for r in await rows(st, "select video_id from impressions where list_id = ?", i)}
        for (i,) in lists_
    ]
    for a, b, c in zip(seen, seen[1:], seen[2:], strict=False):
        assert not (a & b & c)


async def test_impression_kinds_and_metrics(st):
    from serendip import metrics

    def count(kind):
        return metrics.REGISTRY.get_sample_value("serendip_list_items_total", {"kind": kind}) or 0

    before = {k: count(k) for k in ("new", "carried", "reshown")}
    await fill(st, 60, 90)
    await popular(st)
    st.clock.advance(seconds=20)
    await popular(st)
    kinds = dict(await rows(st, "select kind, count(*) from impressions group by kind"))
    assert kinds == {"new": 51, "carried": 9}
    assert count("new") - before["new"] == 51
    assert count("carried") - before["carried"] == 9
    assert await rows(st, "select count(*) from exposures") == [(51,)]
    assert await rows(st, "select max(times_served), max(times_carried) from exposures") == [(2, 1)]


async def test_list_records_seed_and_algorithm_version(st):
    await fill(st, 5, 5)
    await popular(st)
    [(device, seed, version, size)] = await rows(
        st, "select device, seed, algorithm_version, size from lists"
    )
    assert (device, version, size) == ("tv", 1, 10)
    assert len(seed) == 16 and int(seed, 16) >= 0


async def test_device_comes_from_the_user_agent_map(st):
    await fill(st, 5, 5)
    await popular(st, TV)
    await popular(st, PHONE)
    await popular(st, "Mozilla/5.0 plain")
    assert await rows(st, "select device from lists order by id") == [
        ("tv",), ("phone",), ("unknown",)
    ]  # fmt: skip


async def test_new_items_never_repeat_across_devices(st):
    await fill(st, 40, 60)
    tv = await popular(st, TV)
    phone = await popular(st, PHONE)
    assert not set(ids(tv)) & set(ids(phone))


async def test_shortage_returns_a_shorter_list_then_reshows_oldest_first(st):
    await fill(st, 5, 5)
    first = await popular(st)
    assert len(first) == 10
    st.clock.advance(seconds=20)
    second = await popular(st)
    # Nothing new, and the carry ceiling of a list with no other items is zero: the previous
    # list comes back in a new order rather than an empty page.
    assert sorted(ids(second)) == sorted(ids(first)) and ids(second) != ids(first)
    st.clock.advance(seconds=20)
    await seed_related(st, [server_item(2000 + i, channel=2000 + i) for i in range(4)])
    third = await popular(st)
    # 4 new; nothing carried, since every previous item was already in two consecutive lists.
    assert len(third) == 4 and not set(ids(third)) & set(ids(first))
    st.clock.advance(seconds=20)
    fourth = await popular(st)
    # The first list's items are re-shown (they are not in the previous list), oldest first.
    reshown = [i for i in ids(fourth) if i in set(ids(first))]
    assert reshown


# --- repeat window ----------------------------------------------------------------------------


async def test_repeat_inside_the_window_returns_the_same_list_and_records_nothing(st):
    await fill(st, 40, 60)
    first = await popular(st)
    before = await rows(st, "select count(*) from impressions")
    st.clock.advance(seconds=14, microseconds=999_999)
    assert await popular(st) == first
    st.clock.advance(seconds=0)
    assert await rows(st, "select count(*) from lists") == [(1,)]
    assert await rows(st, "select count(*) from impressions") == before
    assert await rows(st, "select max(times_served) from exposures") == [(1,)]


async def test_repeat_does_no_selection_work(st, monkeypatch):
    from serendip import selection

    await fill(st, 40, 60)
    first = await popular(st)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a repeat must not build a list")

    monkeypatch.setattr(selection, "build", forbidden)
    st.clock.advance(seconds=3)
    assert await popular(st) == first


async def test_window_is_fixed_not_sliding(st):
    await fill(st, 60, 90)
    first = await popular(st)
    st.clock.advance(seconds=10)
    assert await popular(st) == first  # a repeat at 10 s...
    st.clock.advance(seconds=6)
    fresh = await popular(st)  # ...does not extend the window: 16 s after the list is new
    assert fresh != first
    assert not set(ids(fresh[:5])) & set(ids(first))
    assert await rows(st, "select count(*) from lists") == [(2,)]


async def test_window_boundary_is_exactly_the_configured_seconds(tmp_path):
    state = await make_state(tmp_path, clock=FakeClock(), repeat_window_s=15)
    try:
        await fill(state, 60, 90)
        first = await popular(state)
        state.clock.advance(seconds=5)
        assert await popular(state) == first
        state.clock.advance(seconds=5)
        assert await popular(state) == first
        state.clock.advance(seconds=4, microseconds=999_999)  # 14.999999 s after the list
        assert await popular(state) == first
        state.clock.advance(microseconds=1)  # exactly 15 s: not "within" any more
        fresh = await popular(state)
        assert fresh != first
        assert await rows(state, "select count(*) from lists") == [(2,)]
    finally:
        await state.db.close()


async def test_repeat_drops_items_opened_since(st):
    await fill(st, 40, 60)
    first = await popular(st)
    st.clock.advance(seconds=3)
    await st.db.record_open("jan", "tv", stamp(st.clock.now()), first[2]["videoId"], None)
    again = await popular(st)
    assert ids(again) == [v for v in ids(first) if v != first[2]["videoId"]]


async def test_repeat_is_per_device_and_unmapped_devices_share_state(st):
    await fill(st, 60, 90)
    tv = await popular(st, TV)
    phone = await popular(st, PHONE)  # another device: its own list, not a repeat
    assert tv != phone
    other = await popular(st, "Mozilla plain")
    again = await popular(st, "Mozilla/5.0 something else entirely")  # also 'unknown'
    assert again == other


async def test_zero_window_never_repeats(tmp_path):
    state = await make_state(tmp_path, clock=FakeClock(), repeat_window_s=0)
    try:
        await fill(state, 60, 90)
        a = await popular(state)
        b = await popular(state)
        assert a != b
    finally:
        await state.db.close()


# --- history ----------------------------------------------------------------------------------


async def test_pruned_impressions_and_expired_pool_never_make_a_served_video_new(st):
    await fill(st, 15, 15)
    served = set(ids(await popular(st)))
    assert len(served) == 30
    # Impressions and lists are retention-pruned, the pool expires and the items are found again.
    st.clock.advance(days=40)
    await st.db.apply_discover_retention(st.clock.now())
    assert await rows(st, "select count(*) from impressions") == [(0,)]
    assert await rows(st, "select count(*) from pool") == [(0,)]
    assert await rows(st, "select count(*) from exposures") == [(30,)]
    # The 40-day-old uploads stay expired; the 15 related items are found again, plus 5 new ones.
    await fill(st, 15, 15)
    await seed_related(st, [server_item(3000 + i, channel=3000 + i) for i in range(5)])
    items = await popular(st)
    new = {r[0] for r in await rows(st, "select video_id from impressions where kind = 'new'")}
    assert new == {video_id(3000 + i) for i in range(5)}
    assert ids(items[:5]) == [i for i in ids(items[:5]) if i in new]  # the head is the new ones
    assert len(items) == 20
    assert {i for i in ids(items) if i not in new} <= served  # the rest is re-shown


async def test_restart_keeps_the_ledger(tmp_path):
    state = await make_state(tmp_path, clock=FakeClock())
    await fill(state, 20, 20)
    first = ids(await popular(state))
    await state.db.close()

    again = await make_state(tmp_path, clock=state.clock)
    try:
        again.clock.advance(seconds=60)
        second = ids(await popular(again))
        # Ten videos were never served; they come first, and nothing served is new again.
        assert not set(second[:5]) & set(first)
        new = await rows(again, "select video_id from impressions where kind = 'new'")
        assert len(new) == len({r[0] for r in new}) == 40
    finally:
        await again.db.close()


# --- concurrency and rollback -----------------------------------------------------------------


async def test_simultaneous_requests_on_two_devices_get_disjoint_new_items(st):
    await fill(st, 60, 90)
    results = await asyncio.gather(*(popular(st, ua) for ua in (TV, PHONE, "plain")))
    sets = [set(ids(r)) for r in results]
    assert all(len(s) == 30 for s in sets)
    assert not (sets[0] & sets[1]) and not (sets[0] & sets[2]) and not (sets[1] & sets[2])
    assert await rows(st, "select max(times_served) from exposures") == [(1,)]


async def test_simultaneous_requests_on_one_device_share_one_list(st):
    await fill(st, 60, 90)
    a, b = await asyncio.gather(popular(st), popular(st))
    assert a == b  # the second is a repeat of the first
    assert await rows(st, "select count(*) from lists") == [(1,)]
    assert await rows(st, "select max(times_served) from exposures") == [(1,)]


async def test_validation_failure_records_nothing(st, monkeypatch):
    await fill(st, 20, 20)
    calls = []

    def reject(item):
        calls.append(item)
        if len(calls) == 7:
            raise ValueError("title missing")

    monkeypatch.setattr(lists, "validate_list_item", reject)
    async with client(st) as c:
        r = await c.get("/api/v1/popular", headers=AUTH)
    assert r.status_code == 500
    assert r.headers["cache-control"] == "private, no-store"
    for table in ("lists", "impressions", "exposures"):
        assert await rows(st, f"select count(*) from {table}") == [(0,)]


async def test_cancellation_after_the_writes_leaves_no_phantom_impressions(st, monkeypatch):
    await fill(st, 20, 20)

    def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    # The last statement of the transaction has run when the result object is built.
    monkeypatch.setattr(lists, "Counter", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await lists.serve_list(st.db, st.settings, "jan", "tv", st.clock.now())
    for table in ("lists", "impressions", "exposures"):
        assert await rows(st, f"select count(*) from {table}") == [(0,)]
    monkeypatch.undo()
    assert len(await popular(st)) == 30  # the lock was released and the items are still new


async def test_cancellation_at_begin_immediate_still_rolls_back(st):
    await fill(st, 20, 20)
    conn = st.db._conn
    real_execute = conn.execute
    cancelled = []

    async def cancel_at_begin(sql, *args, **kwargs):
        if sql == "begin immediate" and not cancelled:
            cancelled.append(sql)
            # aiosqlite runs the queued statement although the awaiting task is cancelled.
            queued = asyncio.ensure_future(real_execute(sql, *args, **kwargs))
            await asyncio.sleep(0)
            asyncio.current_task().cancel()
            await queued
        return await real_execute(sql, *args, **kwargs)

    conn.execute = cancel_at_begin
    task = asyncio.create_task(lists.serve_list(st.db, st.settings, "jan", "tv", st.clock.now()))
    with pytest.raises(asyncio.CancelledError):
        await task
    del conn.execute  # back to the class method
    assert cancelled
    assert conn.in_transaction is False  # the transaction BEGIN opened was rolled back
    for table in ("lists", "impressions", "exposures"):
        assert await rows(st, f"select count(*) from {table}") == [(0,)]
    assert len(await popular(st)) == 30  # the next selection is not inside a stale transaction
    assert conn.in_transaction is False


async def test_no_outbound_http_at_request_time(st, server):
    await fill(st, 40, 60)
    await popular(st)
    await popular(st, PHONE)
    st.clock.advance(seconds=30)
    await popular(st)
    async with client(st) as c:
        await c.get("/status")
    assert len(server.calls) == 0


async def test_status_counts_never_served_eligible_items(st):
    await fill(st, 10, 20)
    async with client(st) as c:
        assert (await c.get("/status")).json()["never_served_eligible"] == {"jan": 30}
    await popular(st)
    async with client(st) as c:
        body = (await c.get("/status")).json()
    assert body["never_served_eligible"] == {"jan": 0}
    assert body["pool"] == {"subs": 10, "related": 0, "adjacent": 20}


def test_stamp_us_sorts_with_microseconds():
    from datetime import UTC, datetime

    a = datetime(2026, 1, 1, 0, 0, 5, 1, tzinfo=UTC)
    b = datetime(2026, 1, 1, 0, 0, 5, 2, tzinfo=UTC)
    assert stamp_us(a) < stamp_us(b)
    assert stamp(a) == "2026-01-01T00:00:05Z"

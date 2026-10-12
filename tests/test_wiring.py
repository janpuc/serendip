"""Startup wiring, supervision of the background jobs, retention."""

import asyncio

from conftest import (
    START,
    FakeClock,
    Worker,
    make_state,
    route_video,
    rows,
    seed_related,
    seed_subs,
    server_item,
    video_id,
    video_response,
    wait_idle,
)
from test_popular import client

from serendip import server
from serendip.db import stamp


async def test_without_a_service_user_background_jobs_are_off(tmp_path):
    state = await make_state(tmp_path, clock=FakeClock())
    try:
        assert server._start_jobs(state) is None
        assert state.feed is None and state.followups is None
        async with client(state) as c:
            body = (await c.get("/status")).json()
        assert body["service_user_configured"] is False
        assert body["background_jobs"] == "disabled: no service user"
        # /popular still serves from what is in the database.
        await seed_subs(state, [server_item(i, channel=i) for i in range(5)])
        async with client(state) as c:
            from conftest import basic

            r = await c.get("/api/v1/popular", headers={"Authorization": basic()})
        assert len(r.json()) == 5
    finally:
        await state.db.close()


async def test_with_a_service_user_both_jobs_start(tmp_path):
    state = await make_state(
        tmp_path, yattee_username="serendip", yattee_password="pw", clock=FakeClock()
    )
    try:
        job_client = server._start_jobs(state)
        assert job_client is not None
        assert state.feed is not None and state.followups is not None
        await job_client.close()
    finally:
        await state.db.close()


async def test_related_source_off_starts_only_the_feed_job(tmp_path):
    state = await make_state(
        tmp_path,
        yattee_username="serendip",
        yattee_password="pw",
        related_source="off",
        clock=FakeClock(),
    )
    try:
        job_client = server._start_jobs(state)
        assert state.feed is not None and state.followups is None
        await job_client.close()
        async with client(state) as c:
            assert (await c.get("/status")).json()["related_source"] == "off"
    finally:
        await state.db.close()


async def test_a_crashed_followup_worker_makes_the_process_unhealthy(jobs_state, monkeypatch):
    state = jobs_state
    stopped: list[bool] = []

    async def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(state.followups, "run", broken)
    task = asyncio.create_task(state.followups.run(), name="followups")
    server.supervise(task, state, lambda: stopped.append(True))
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert stopped == [True] and state.healthy is False


async def test_a_failing_followup_does_not_end_the_worker(jobs_state, server, monkeypatch):
    state = jobs_state
    route_video(server, 2, json=video_response(2, []))

    async def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    calls = []
    original = state.db.store_related

    async def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return await boom()
        return await original(*args, **kwargs)

    monkeypatch.setattr(state.db, "store_related", flaky)
    route_video(server, 1, json=video_response(1, []))
    async with Worker(state.followups):
        await state.followups.trigger("jan", video_id(1), START)
        await wait_idle(state.followups)
        state.clock.advance(minutes=20)
        await state.followups.trigger("jan", video_id(2), state.clock.now())
        await wait_idle(state.followups)
    assert len(calls) == 2


async def test_a_seed_found_live_in_a_related_list_is_skipped_later(jobs_state, server):
    state = jobs_state
    await seed_related(state, [server_item(5, liveNow=True, length=0)])
    assert await state.followups.trigger("jan", video_id(5), START) == "skipped_live"
    assert len(server.calls) == 0


async def test_discover_retention_expires_but_keeps_exposures(tmp_path):
    state = await make_state(tmp_path, clock=FakeClock())
    try:
        await seed_subs(state, [server_item(i, channel=i) for i in range(5)])
        from conftest import basic

        async with client(state) as c:
            await c.get("/api/v1/popular", headers={"Authorization": basic()})
        await state.db.followup_reserve(video_id(1), stamp(state.clock.now()))
        state.clock.advance(days=31)
        deleted = await state.db.apply_discover_retention(state.clock.now())
        assert deleted["pool"] == 5 and deleted["impressions"] == 5 and deleted["lists"] == 1
        assert deleted["followups"] == 1 and deleted["videos"] == 5
        assert await rows(state, "select count(*) from exposures") == [(5,)]
        assert await rows(state, "select count(*) from videos") == [(0,)]
    finally:
        await state.db.close()


async def test_recent_lists_and_their_videos_survive_retention(tmp_path):
    state = await make_state(tmp_path, clock=FakeClock())
    try:
        await seed_subs(state, [server_item(i, channel=i) for i in range(5)])
        from conftest import basic

        async with client(state) as c:
            await c.get("/api/v1/popular", headers={"Authorization": basic()})
        state.clock.advance(days=20)
        deleted = await state.db.apply_discover_retention(state.clock.now())
        assert deleted == {
            "pool": 0, "impressions": 0, "lists": 0, "followups": 0, "feed_runs": 0, "videos": 0,
        }  # fmt: skip
    finally:
        await state.db.close()

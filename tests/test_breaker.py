"""The breaker (DESIGN 7): what trips it, what does not, persistence, reset, silence while open."""

import asyncio
import json

import httpx
import pytest
import structlog
from conftest import (
    Worker,
    make_state,
    route_video,
    rows,
    seed_subs,
    server_item,
    video_id,
    video_response,
    wait_idle,
)

from serendip import cli, logs, metrics
from serendip.breaker import BOT_CHECK, ERROR, MANUAL, OPERATIONAL_STOP

BOT = "Sign in to confirm you’re not a bot"
BOT_ESCAPED = json.dumps({"detail": BOT})  # ensure_ascii writes the apostrophe as \u2019


def reply(**kwargs) -> dict:
    return kwargs


async def follow(state, n: int = 1) -> None:
    await state.followups.trigger("jan", video_id(n), state.clock.now())
    await wait_idle(state.followups)


async def breaker(state):
    return await state.breaker.state()


TRIPS = [
    pytest.param(reply(status_code=404, content=BOT_ESCAPED), BOT_CHECK, id="bot-text-in-404"),
    pytest.param(
        reply(status_code=200, json=video_response(1, [], description=BOT)), BOT_CHECK, id="bot-text-in-200"
    ),
    pytest.param(reply(status_code=200, content=f"<html>{BOT}</html>"), BOT_CHECK, id="bot-text-in-html-200"),
    pytest.param(reply(status_code=500, content="LOGIN_REQUIRED"), BOT_CHECK, id="login-required"),
    pytest.param(reply(status_code=200, content="not json"), ERROR, id="malformed-json"),
    pytest.param(reply(status_code=200, content=""), ERROR, id="empty-body"),
    pytest.param(reply(status_code=200, json=["a"]), ERROR, id="wrong-shape"),
    pytest.param(reply(status_code=200, json=None), ERROR, id="null-json"),
    pytest.param(reply(status_code=200, json=video_response(1, "oops")), ERROR, id="related-not-a-list"),
    pytest.param(reply(status_code=200, json=video_response(2, [])), ERROR, id="other-video-id"),
    pytest.param(reply(status_code=401), OPERATIONAL_STOP, id="401"),
    pytest.param(reply(status_code=403), OPERATIONAL_STOP, id="403"),
    pytest.param(reply(status_code=429), OPERATIONAL_STOP, id="429"),
    pytest.param(reply(status_code=500), ERROR, id="500"),
    pytest.param(reply(status_code=502), ERROR, id="502"),
    pytest.param(reply(status_code=503), ERROR, id="503"),
    pytest.param(reply(status_code=422, json={"detail": "Could not extract video"}), ERROR, id="422"),
    pytest.param(reply(status_code=400, json={"detail": "bad"}), ERROR, id="400"),
    pytest.param(reply(status_code=302, headers={"location": "http://elsewhere.test/"}), ERROR, id="redirect"),
]  # fmt: skip


@pytest.mark.parametrize("response, kind", TRIPS)
async def test_response_that_trips_the_breaker(jobs_state, server, response, kind):
    state = jobs_state
    route = route_video(server, 1, **response)
    async with Worker(state.followups):
        await follow(state)
    assert route.call_count == 1
    status = await breaker(state)
    assert status.open and status.kind == kind and status.reason
    assert metrics.REGISTRY.get_sample_value("serendip_breaker_open") == 1
    assert await rows(state, "select status from followups") == [("tripped",)]
    # The reason is ours: no response text, no credential, no URL.
    assert BOT not in status.reason and "s3cret" not in status.reason
    assert "elsewhere" not in status.reason
    # Related items from a response that tripped are not stored.
    assert await rows(state, "select count(*) from pool") == [(0,)]


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("eof")],
)
async def test_transport_errors_and_timeouts_trip_it(jobs_state, server, error):
    server.get(f"/api/v1/videos/{video_id(1)}").mock(side_effect=error)
    async with Worker(jobs_state.followups):
        await follow(jobs_state)
    status = await breaker(jobs_state)
    assert status.open and status.kind == ERROR


async def test_total_timeout_trips_it(make_jobs, server):
    state = await make_jobs(server_timeout_s=0.05)

    async def hang(_request):
        await asyncio.sleep(5)

    server.get(f"/api/v1/videos/{video_id(1)}").mock(side_effect=hang)
    async with Worker(state.followups):
        await follow(state)
    status = await breaker(state)
    assert status.open and status.reason == "video: timeout"


async def test_plain_404_marks_only_that_seed_failed(jobs_state, server):
    state = jobs_state
    route_video(server, 1, status_code=404, json={"detail": "Video not found"})
    route_video(server, 2, json=video_response(2, [server_item(200)]))
    async with Worker(state.followups):
        await follow(state, 1)
        state.clock.advance(minutes=11)
        await follow(state, 2)
    assert not (await breaker(state)).open
    assert await rows(state, "select video_id, status from followups order by id") == [
        (video_id(1), "not_found"),
        (video_id(2), "ok"),
    ]
    assert metrics.REGISTRY.get_sample_value("serendip_breaker_open") == 0


async def test_titles_that_mention_sign_in_or_bots_do_not_trip_it(jobs_state, server):
    related = [server_item(200, title="How to sign in to Discord and build a bot")]
    route_video(server, 1, json=video_response(1, related))
    async with Worker(jobs_state.followups):
        await follow(jobs_state)
    assert not (await breaker(jobs_state)).open
    assert await rows(jobs_state, "select count(*) from pool") == [(1,)]


async def test_the_first_reason_is_kept_and_status_shows_it(jobs_state, server):
    state = jobs_state
    await state.breaker.trip(OPERATIONAL_STOP, "video: HTTP 429")
    assert not await state.breaker.trip(ERROR, "video: timeout")
    status = await breaker(state)
    assert (status.kind, status.reason) == (OPERATIONAL_STOP, "video: HTTP 429")
    from test_popular import client

    async with client(state) as c:
        body = (await c.get("/status")).json()
    assert body["breaker"]["open"] is True
    assert body["breaker"]["reason"] == "video: HTTP 429"
    async with client(state) as c:
        assert "serendip_breaker_open 1.0" in (await c.get("/metrics")).text


async def test_open_breaker_persists_across_restart_and_blocks_every_call(
    make_jobs, server, tmp_path
):
    state = await make_jobs()
    await seed_subs(state, [server_item(1, channel=1)])
    await state.breaker.trip(BOT_CHECK, "video: bot")
    await state.client.close()
    await state.db.close()

    again = await make_jobs()  # a new process on the same database file
    assert (await breaker(again)).open
    async with Worker(again.followups):
        await follow(again)
    assert await again.feed.run_due() == "paused_breaker"
    assert len(server.calls) == 0  # not a single request while it is open
    assert await rows(again, "select count(*) from followups") == [(0,)]
    assert await rows(again, "select count(*) from feed_runs") == [(0,)]


async def test_reset_resumes_calls(jobs_state, server):
    state = jobs_state
    route = route_video(server, 1, json=video_response(1, []))
    await state.breaker.trip(ERROR, "video: HTTP 503")
    async with Worker(state.followups):
        await follow(state)
        assert route.call_count == 0
        assert await state.breaker.reset()
        assert not await state.breaker.reset()
        state.clock.advance(minutes=20)
        await follow(state)
    assert route.call_count == 1
    assert metrics.REGISTRY.get_sample_value("serendip_breaker_open") == 0


def test_cli_trip_and_reset_work_on_the_database_file(tmp_path, monkeypatch, capsys):
    from serendip import migrate
    from serendip.db import Database

    path = str(tmp_path / "b.db")
    migrate.migrate(path)
    monkeypatch.setenv("SERENDIP_DB_PATH", path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["breaker", "trip", "--reason", "maintenance, see https://x/y?sig=SECRET"]) == 0
    out = capsys.readouterr().out
    assert "breaker tripped" in out and "SECRET" not in out

    async def read():
        db = await Database.connect(path)
        try:
            return await db.breaker_state()
        finally:
            await db.close()

    status = asyncio.run(read())
    assert status.open and status.kind == MANUAL and "SECRET" not in status.reason
    assert cli.main(["breaker", "trip", "--reason", "again"]) == 0
    assert "already open" in capsys.readouterr().out
    assert cli.main(["breaker", "reset"]) == 0
    assert "breaker reset" in capsys.readouterr().out
    assert not asyncio.run(read()).open
    assert cli.main(["breaker", "reset"]) == 0
    assert "was not open" in capsys.readouterr().out


async def test_cli_reset_takes_effect_in_a_running_state(tmp_path):
    state = await make_state(tmp_path)
    try:
        await state.breaker.trip(ERROR, "x")
        assert await state.breaker.is_open()
        from serendip.db import Database

        other = await Database.connect(state.settings.db_path)  # "another process"
        await other.reset_breaker()
        await other.close()
        assert not await state.breaker.is_open()
    finally:
        await state.db.close()


async def test_credentials_never_reach_logs_the_database_or_reprs(jobs_state, server, tmp_path):
    state = jobs_state
    lines: list[str] = []
    logs.configure("DEBUG", renderer=lambda _l, _m, ed: lines.append(str(ed)) or "")
    try:
        server.get(f"/api/v1/videos/{video_id(1)}").mock(
            side_effect=httpx.ConnectError("refused at http://yattee.test/?sig=SECRET")
        )
        async with Worker(state.followups):
            await follow(state)
        structlog.get_logger().info("probe", settings=state.settings)
    finally:
        logs.configure("INFO")
    joined = "\n".join(lines)
    assert lines and "breaker_tripped" in joined
    for secret in ("s3cret-password", "czNjcmV0LXBhc3N3b3Jk", "Basic ", "SECRET"):
        assert secret not in joined
        assert secret not in repr(state.settings)
        assert secret not in repr(state.followups)
    await state.db.checkpoint()
    for path in tmp_path.glob("serendip.db*"):
        data = path.read_bytes()
        assert b"s3cret-password" not in data
        assert b"czNjcmV0LXBhc3N3b3Jk" not in data


async def test_start_open_opens_the_breaker_on_the_first_start_only(tmp_path):
    state = await make_state(tmp_path)
    try:
        assert not (await breaker(state)).open  # a fresh breaker row starts closed
        assert await state.breaker.initialise(True)
        status = await breaker(state)
        assert status.open and status.kind == MANUAL
        assert status.reason == "initial: confirm before enabling jobs"
        assert metrics.REGISTRY.get_sample_value("serendip_breaker_open") == 1
        # A later start does nothing, and a reset is not undone by one.
        assert not await state.breaker.initialise(True)
        assert await state.breaker.reset()
        assert not await state.breaker.initialise(True)
        assert not (await breaker(state)).open
    finally:
        await state.db.close()


async def test_start_open_off_leaves_it_closed_and_later_starts_do_not_open_it(tmp_path):
    state = await make_state(tmp_path)
    try:
        assert not await state.breaker.initialise(False)
        assert not (await breaker(state)).open
        assert not await state.breaker.initialise(True)  # the first start already happened
        assert not (await breaker(state)).open
    finally:
        await state.db.close()


async def test_start_open_stops_every_call_until_reset(make_jobs, server):
    state = await make_jobs(breaker_start_open=True)
    assert await state.breaker.initialise(state.settings.breaker_start_open)
    await seed_subs(state, [server_item(1, channel=1)])
    async with Worker(state.followups):
        await follow(state)
    assert await state.feed.run_due() == "paused_breaker"
    assert len(server.calls) == 0

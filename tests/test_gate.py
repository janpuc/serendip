"""The one gate every call to Yattee Server goes through (DESIGN 7): breaker, deadline and clocks
checked at the moment of sending, and a trip that lands before the next call can go."""

import asyncio
from datetime import timedelta

import httpx
from conftest import START, route_video, video_id, video_response
from structlog.testing import capture_logs

from serendip import metrics
from serendip.upstream import Reply


def skipped(reason: str) -> float:
    return (
        metrics.REGISTRY.get_sample_value("serendip_sends_skipped_total", {"reason": reason}) or 0
    )


async def get(state, n: int = 1, **kwargs) -> Reply:
    return await state.client.request("video", "GET", f"/api/v1/videos/{video_id(n)}", **kwargs)


async def test_an_open_breaker_stops_the_send(jobs_state, server):
    state = jobs_state
    route = route_video(server, 1, json=video_response(1, []))
    await state.breaker.trip("manual", "maintenance")
    before = skipped("breaker")
    reply = await get(state)
    assert reply.skipped == "breaker" and reply.trip is None and reply.status is None
    assert route.call_count == 0
    assert skipped("breaker") == before + 1


async def test_a_deadline_that_has_passed_stops_the_send(jobs_state, server):
    state = jobs_state
    route = route_video(server, 1, json=video_response(1, []))
    reply = await get(state, deadline=START - timedelta(seconds=1))
    assert reply.skipped == "deadline" and route.call_count == 0
    assert (
        await get(state, deadline=START)
    ).skipped is None  # the deadline itself is still in time
    assert route.call_count == 1


async def test_a_tripping_reply_closes_the_gate_before_the_next_call(jobs_state, server):
    state = jobs_state

    async def slow_503(_request):
        await asyncio.sleep(0.05)
        return httpx.Response(503)

    route = server.get(f"/api/v1/videos/{video_id(1)}").mock(side_effect=slow_503)
    first = asyncio.create_task(get(state))
    await asyncio.sleep(0.01)  # the first call is in flight; the second queues at the gate
    second = asyncio.create_task(state.client.request("feed", "POST", "/api/v1/feed", json_body={}))
    first_reply, second_reply = await asyncio.gather(first, second)
    assert first_reply.trip == ("error", "video: HTTP 503")
    assert second_reply.skipped == "breaker"
    assert route.call_count == 1 and len(server.calls) == 1
    assert (await state.breaker.state()).reason == "video: HTTP 503"


async def test_a_body_the_caller_refuses_is_malformed_and_trips(jobs_state, server):
    state = jobs_state
    route_video(server, 1, json={"unexpected": True})
    reply = await get(state, accept=lambda data: isinstance(data, dict) and "videoId" in data)
    assert reply.trip == ("error", "video: malformed")
    assert (await state.breaker.state()).open


async def test_clocks_that_disagree_by_more_than_a_minute_stop_the_send(jobs_state, server):
    state = jobs_state
    route = route_video(server, 1, json=video_response(1, []))
    assert (await get(state)).ok
    state.clock.advance(minutes=5)  # both clocks agree: fine
    state.clock.jump_wall(seconds=59)  # within the tolerance
    assert (await get(state)).ok
    before = skipped("clock")
    state.clock.jump_wall(seconds=61)
    with capture_logs() as logged:
        reply = await get(state)
    assert reply.skipped == "clock" and route.call_count == 2
    assert skipped("clock") == before + 1
    assert [e["event"] for e in logged] == ["send_skipped_clock_disagreement"]
    assert logged[0]["log_level"] == "warning"
    # Still stopped: a disagreement is not forgotten until the process restarts.
    state.clock.advance(minutes=30)
    assert (await get(state)).skipped == "clock"


async def test_a_backward_wall_clock_jump_stops_the_send(jobs_state, server):
    state = jobs_state
    route = route_video(server, 1, json=video_response(1, []))
    assert (await get(state)).ok
    state.clock.jump_wall(minutes=-5)
    assert (await get(state)).skipped == "clock"
    assert route.call_count == 1

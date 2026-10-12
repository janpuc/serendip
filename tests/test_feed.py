"""The feed job (DESIGN 5): POST /api/v1/feed as the service user, per user."""

import json
from datetime import timedelta

import httpx
import pytest
from conftest import (
    basic,
    channel_id,
    rows,
    seed_related,
    server_item,
    video_id,
)

from serendip import metrics
from serendip.db import stamp, stamp_us
from serendip.feed import FeedJob


def feed_response(items, status="ready", **extra):
    return {
        "status": status,
        "videos": items,
        "total": len(items),
        "has_more": False,
        "ready_count": 1,
        "pending_count": 0,
        **extra,
    }


async def subscribe(state, n: int, name: str = "jan", *, seen=None) -> None:
    ts = stamp(seen or state.clock.now())
    await state.db.record_subscriptions(
        name, "tv", ts, [(channel_id(i), f"Channel {i}") for i in range(1, n + 1)]
    )


def jobs(outcome: str) -> float:
    return (
        metrics.REGISTRY.get_sample_value(
            "serendip_jobs_total", {"job": "feed", "outcome": outcome}
        )
        or 0
    )


async def test_feed_post_stores_uploads_in_the_subs_pool(jobs_state, server):
    state = jobs_state
    await subscribe(state, 3)
    await state.db.record_open("jan", "tv", stamp(state.clock.now()), video_id(1), None)
    items = [server_item(1, channel=1), server_item(2, channel=2), {"videoId": "broken"}]
    route = server.post("/api/v1/feed").respond(json=feed_response(items))
    events_before = await rows(state, "select count(*) from events")

    assert await state.feed.run_due() == "ready"

    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["authorization"] == basic("serendip", "s3cret-password")
    body = json.loads(request.content)
    assert body == {
        "channels": [{"channel_id": channel_id(i), "site": "youtube"} for i in (1, 2, 3)],
        "limit": 1000,
        "offset": 0,
    }
    assert await rows(state, "select video_id, source from pool order by 1") == [
        (video_id(1), "subs"),
        (video_id(2), "subs"),
    ]
    # An opened video learns its channel from the feed.
    assert await rows(state, "select channel_id from opens") == [(channel_id(1),)]
    # serendip's own post is not an observed subscription event.
    assert await rows(state, "select count(*) from events") == events_before
    [run] = await rows(state, "select status, channels, items, truncated from feed_runs")
    assert run == ("ready", 3, 2, 0)
    assert jobs("ready") >= 1


async def test_only_active_subscriptions_are_posted_one_chunk_per_interval(jobs_state, server):
    state = jobs_state
    now = state.clock.now()
    ts = stamp(now)
    await state.db.record_subscriptions(
        "jan", "tv", ts, [(channel_id(i), None) for i in range(1, 1202)]
    )
    await state.db.record_subscriptions(
        "jan", "tv", stamp(now - timedelta(days=31)), [(channel_id(5000), None)]
    )  # unseen for 31 days: not posted
    route = server.post("/api/v1/feed").respond(json=feed_response([]))

    assert await state.feed.run_due() == "ready"
    assert route.call_count == 1  # one chunk now, not three
    assert await state.feed.run_due() is None
    state.clock.advance(hours=6)
    assert await state.feed.run_due() == "ready"
    state.clock.advance(hours=6)
    assert await state.feed.run_due() == "ready"
    state.clock.advance(hours=6)
    assert await state.feed.run_due() == "ready"  # the cursor wrapped to the first chunk

    bodies = [json.loads(c.request.content) for c in route.calls]
    sizes = [len(b["channels"]) for b in bodies]
    assert sizes == [500, 500, 201, 500]
    posted = {ch["channel_id"] for b in bodies[:3] for ch in b["channels"]}
    assert channel_id(5000) not in posted and len(posted) == 1201
    assert bodies[3]["channels"] == bodies[0]["channels"]
    assert all(b["limit"] == 1000 for b in bodies)
    assert await rows(state, "select chunk_offset, channels from feed_runs order by id") == [
        (0, 500),
        (500, 500),
        (1000, 201),
        (0, 500),
    ]


async def test_one_post_per_interval_across_users_taken_in_turn(jobs_state, server):
    state = jobs_state
    await subscribe(state, 2, "jan")
    await state.db.record_subscriptions(
        "bob", "tv", stamp(state.clock.now()), [(channel_id(900 + i), None) for i in range(3)]
    )
    route = server.post("/api/v1/feed").respond(json=feed_response([]))

    users = []
    for _ in range(4):
        assert await state.feed.run_due() == "ready"
        assert await state.feed.run_due() is None  # nothing more until the interval is over
        state.clock.advance(hours=6)
    users = await rows(
        state, "select u.name from feed_runs f join users u on u.id = f.user_id order by f.id"
    )
    assert users == [("bob",), ("jan",), ("bob",), ("jan",)]
    assert route.call_count == 4


async def test_reservation_is_written_before_the_post_and_survives_a_crash_loop(jobs_state, server):
    state = jobs_state
    await subscribe(state, 1)
    seen = []

    def crash(_request):
        seen.append(None)
        raise RuntimeError("process died")

    server.post("/api/v1/feed").mock(side_effect=crash)

    async def reservations():
        return await rows(state, "select status, chunk_offset from feed_runs")

    with pytest.raises(RuntimeError):
        await state.feed.run_due()
    assert await reservations() == [("failed", 0)]
    # A "restart" in a crash loop, any number of times: no second post inside the interval.
    for _ in range(3):
        again = FeedJob(
            state.settings, state.db, state.breaker, state.clock, state.followups.client
        )
        assert await again.run_due() is None
    assert len(seen) == 1
    # Even a reservation with no outcome at all (killed before the reply) holds the interval.
    await state.db._conn.execute("update feed_runs set status = 'fetching', finished_at = null")
    await state.db._conn.commit()
    fresh = FeedJob(state.settings, state.db, state.breaker, state.clock, state.followups.client)
    assert await fresh.run_due() is None


async def test_limit_comes_from_settings(make_jobs, server):
    state = await make_jobs(feed_limit=250)
    await subscribe(state, 1)
    route = server.post("/api/v1/feed").respond(json=feed_response([]))
    await state.feed.run_due()
    assert json.loads(route.calls[0].request.content)["limit"] == 250


async def test_truncation_and_fetching_status_are_recorded(jobs_state, server):
    state = jobs_state
    await subscribe(state, 2)
    server.post("/api/v1/feed").respond(
        json=feed_response([server_item(1)], status="fetching", has_more=True, pending_count=1)
    )
    assert await state.feed.run_due() == "fetching"
    [run] = await rows(state, "select status, items, truncated from feed_runs")
    assert run == ("fetching", 1, 1)
    assert await rows(state, "select count(*) from pool") == [(1,)]  # what was ready is used


async def test_old_uploads_are_not_pooled_and_expiry_follows_the_upload(jobs_state, server):
    state = jobs_state
    await subscribe(state, 1)
    old = int((state.clock.now() - timedelta(days=31)).timestamp())
    recent = int((state.clock.now() - timedelta(days=10)).timestamp())
    server.post("/api/v1/feed").respond(
        json=feed_response(
            [
                server_item(1, published=old),
                server_item(2, published=recent),
                server_item(3, published=None, publishedText=None),
            ]
        )
    )
    await state.feed.run_due()
    pooled = dict(await rows(state, "select video_id, expires_at from pool"))
    assert set(pooled) == {video_id(2), video_id(3)}
    assert pooled[video_id(2)] == stamp(state.clock.now() + timedelta(days=20))
    assert pooled[video_id(3)] == stamp(state.clock.now() + timedelta(days=30))


async def test_a_video_in_the_feed_and_in_related_lists_counts_as_subs(jobs_state, server):
    state = jobs_state
    await subscribe(state, 1)
    await seed_related(state, [server_item(1, channel=1), server_item(2, channel=1)])
    server.post("/api/v1/feed").respond(json=feed_response([server_item(1, channel=1)]))
    await state.feed.run_due()
    assert await rows(state, "select video_id, source, seed_video_id from pool order by 1") == [
        (video_id(1), "subs", None),
        (video_id(2), "related", "seed0000000"),
    ]
    # And a later related sighting does not demote it.
    await seed_related(state, [server_item(1, channel=1)])
    assert await rows(state, "select source from pool where video_id = ?", video_id(1)) == [
        ("subs",)
    ]


async def test_runs_at_most_once_per_interval_even_across_restarts(jobs_state, server):
    state = jobs_state
    await subscribe(state, 1)
    route = server.post("/api/v1/feed").respond(json=feed_response([]))

    await state.feed.run_due()
    assert route.call_count == 1
    state.clock.advance(hours=5, minutes=59)
    await state.feed.run_due()
    assert route.call_count == 1
    # A restart within the interval does not post again either.
    restarted = FeedJob(
        state.settings, state.db, state.breaker, state.clock, state.followups.client
    )
    await restarted.run_due()
    assert route.call_count == 1
    state.clock.advance(minutes=2)
    await restarted.run_due()
    assert route.call_count == 2


async def test_users_without_active_subscriptions_are_not_posted(jobs_state, server):
    state = jobs_state
    await subscribe(state, 2, seen=state.clock.now() - timedelta(days=40))
    await state.feed.run_due()
    assert len(server.calls) == 0
    assert await state.feed.run_due() is None
    assert len(server.calls) == 0


async def test_server_error_trips_the_breaker_and_stops_the_remaining_chunks(jobs_state, server):
    state = jobs_state
    await state.db.record_subscriptions(
        "jan", "tv", stamp(state.clock.now()), [(channel_id(i), None) for i in range(1, 1002)]
    )
    route = server.post("/api/v1/feed").mock(
        side_effect=[httpx.Response(200, json=feed_response([server_item(1)])), httpx.Response(503)]
    )
    assert await state.feed.run_due() == "ready"
    state.clock.advance(hours=6)
    assert await state.feed.run_due() == "tripped"
    assert route.call_count == 2  # the third chunk is never sent
    status = await state.breaker.state()
    assert status.open and status.reason == "feed: HTTP 503"
    assert await rows(state, "select status from feed_runs order by id") == [
        ("ready",),
        ("tripped",),
    ]
    assert await rows(state, "select count(*) from pool") == [(1,)]
    # While open, nothing more is posted, and the next due run is skipped too.
    state.clock.advance(hours=7)
    assert await state.feed.run_due() == "paused_breaker"
    assert route.call_count == 2


async def test_malformed_feed_response_trips_the_breaker(jobs_state, server):
    state = jobs_state
    await subscribe(state, 1)
    server.post("/api/v1/feed").respond(json={"status": "ready", "videos": "nope"})
    assert await state.feed.run_due() == "tripped"
    assert (await state.breaker.state()).reason == "feed: malformed"


async def test_bot_text_in_a_feed_response_trips_the_breaker(jobs_state, server):
    state = jobs_state
    await subscribe(state, 1)
    server.post("/api/v1/feed").respond(
        json=feed_response([server_item(1, title="Sign in to confirm you're not a bot")])
    )
    assert await state.feed.run_due() == "tripped"
    assert (await state.breaker.state()).kind == "bot_check"
    assert await rows(state, "select count(*) from pool") == [(0,)]


async def test_a_failed_run_still_counts_toward_the_interval(jobs_state, server, monkeypatch):
    state = jobs_state
    await subscribe(state, 1)
    server.post("/api/v1/feed").respond(json=feed_response([server_item(1)]))

    async def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(state.db, "store_feed", boom)
    with pytest.raises(RuntimeError):
        await state.feed.run_due()
    assert await rows(state, "select status from feed_runs") == [("failed",)]
    calls = len(server.calls)
    await state.feed.run_due()  # the interval has not passed: no second attempt
    assert len(server.calls) == calls


# --- the send boundary and time accounting -----------------------------------------------------


async def test_breaker_tripped_between_reservation_and_post_sends_nothing(
    jobs_state, server, monkeypatch
):
    state = jobs_state
    await subscribe(state, 2)
    route = server.post("/api/v1/feed").respond(json=feed_response([]))
    real_reserve = state.db.feed_reserve

    async def trip_after(*args):
        run_id = await real_reserve(*args)
        await state.breaker.trip("manual", "stopped from the CLI")
        return run_id

    monkeypatch.setattr(state.db, "feed_reserve", trip_after)
    assert await state.feed.run_due() == "paused_breaker"
    assert route.call_count == 0 and len(server.calls) == 0
    # The reservation stands, so closing the breaker does not bring the next post forward.
    assert await rows(state, "select status, chunk_offset from feed_runs") == [("skipped", 0)]
    monkeypatch.setattr(state.db, "feed_reserve", real_reserve)
    await state.breaker.reset()
    state.clock.advance(hours=5)
    assert await state.feed.run_due() is None
    state.clock.advance(hours=1)
    assert await state.feed.run_due() == "ready"
    assert route.call_count == 1


async def test_the_interval_ends_at_the_exact_microsecond(jobs_state, server):
    state = jobs_state
    await subscribe(state, 1)
    route = server.post("/api/v1/feed").respond(json=feed_response([]))
    state.clock.advance(microseconds=500_000)
    await state.feed.run_due()
    assert route.call_count == 1
    [(started,)] = await rows(state, "select started_at from feed_runs")
    assert started == stamp_us(state.clock.now())  # not truncated to the second
    state.clock.advance(hours=6, microseconds=-1)
    assert await state.feed.run_due() is None  # whole seconds would have posted by now
    state.clock.advance(microseconds=1)
    assert await state.feed.run_due() == "ready"
    assert route.call_count == 2


async def test_a_forward_wall_clock_jump_does_not_bring_the_next_post_forward(jobs_state, server):
    state = jobs_state
    await subscribe(state, 1, seen=state.clock.now() + timedelta(days=30))
    route = server.post("/api/v1/feed").respond(json=feed_response([]))
    await state.feed.run_due()
    state.clock.jump_wall(hours=7)
    assert await state.feed.run_due() is None
    assert route.call_count == 1


async def test_a_backward_wall_clock_jump_does_not_bring_the_next_post_forward(jobs_state, server):
    state = jobs_state
    await subscribe(state, 1)
    route = server.post("/api/v1/feed").respond(json=feed_response([]))
    await state.feed.run_due()
    state.clock.jump_wall(hours=-8)
    assert await state.feed.run_due() is None
    state.clock.advance(hours=7)  # 7 h of real time, but the stored time is 8 h ahead
    assert await state.feed.run_due() is None
    # Monotonic time alone is not enough either: both must have passed.
    assert route.call_count == 1


async def test_clocks_that_disagree_after_a_post_stop_the_next_one(jobs_state, server):
    state = jobs_state
    await subscribe(state, 1)
    route = server.post("/api/v1/feed").respond(json=feed_response([]))
    await state.feed.run_due()
    state.clock.jump_wall(hours=1)
    state.clock.advance(hours=6)  # both clocks now say the interval is over; they disagree by 1 h
    assert await state.feed.run_due() == "skipped_clock"
    assert route.call_count == 1
    assert await rows(state, "select status from feed_runs order by id") == [
        ("ready",),
        ("skipped",),
    ]

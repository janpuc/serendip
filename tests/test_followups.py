"""Follow-ups (DESIGN 5.1, 7) against a fake Yattee Server."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from conftest import (
    START,
    Worker,
    basic,
    channel_id,
    make_state,
    route_video,
    rows,
    seed_subs,
    server_item,
    video_id,
    video_response,
    wait_idle,
)

from serendip import metrics
from serendip.db import stamp, stamp_us
from serendip.followups import budget_left

SEED = 1


def jobs(outcome: str) -> float:
    return (
        metrics.REGISTRY.get_sample_value(
            "serendip_jobs_total", {"job": "followup", "outcome": outcome}
        )
        or 0
    )


async def run_one(state, n: int = SEED, user: str = "jan", *, at=None) -> str:
    """Triggers a follow-up for video n and lets the worker deal with it."""
    outcome = await state.followups.trigger(user, video_id(n), at or state.clock.now())
    await wait_idle(state.followups)
    return outcome


async def test_followup_stores_the_seed_and_related_items(jobs_state, server):
    state = jobs_state
    await seed_subs(state, [server_item(100, channel=1), server_item(101, channel=1)])
    related = [
        server_item(200, channel=1),  # a channel the user follows
        server_item(201, channel=77),  # one they do not
        server_item(100, channel=1),  # already in the subscriptions pool
        {"videoId": "bad"},  # one bad item does not spoil the others
    ]
    route = route_video(server, SEED, json=video_response(SEED, related, authorId=channel_id(5)))
    await state.db.record_open("jan", "tv", stamp(state.clock.now()), video_id(SEED), None)
    before = jobs("ok")

    async with Worker(state.followups):
        await run_one(state)

    assert route.call_count == 1
    request = route.calls[0].request
    assert request.url.query == b""  # no query parameters
    assert request.headers["authorization"] == basic("serendip", "s3cret-password")
    assert jobs("ok") == before + 1
    pool = dict(await rows(state, "select video_id, source from pool"))
    assert pool[video_id(200)] == "related"
    assert pool[video_id(201)] == "adjacent"
    assert pool[video_id(100)] == "subs"  # in both: counts as subs
    assert video_id(SEED) not in pool  # the seed itself was opened
    assert (
        await rows(state, "select seed_video_id from pool where video_id = ?", video_id(201))
    ) == [(video_id(SEED),)]
    # The seed's metadata is stored and the open learns its channel.
    assert await rows(state, "select author_id from videos where video_id = ?", video_id(SEED)) == [
        (channel_id(5),)
    ]
    assert await rows(state, "select channel_id from opens") == [(channel_id(5),)]
    [row] = await rows(
        state,
        "select video_id, status, extraction_method, related_count, seconds is not null,"
        " sent_at is not null from followups",
    )
    assert row == (video_id(SEED), "ok", "hybrid", 3, 1, 1)
    assert metrics.REGISTRY.get_sample_value("serendip_followup_seconds_count") >= 1


async def test_followup_is_attempted_about_60_seconds_after_the_open(jobs_state, server):
    state = jobs_state
    route_video(server, SEED, json=video_response(SEED, []))
    async with Worker(state.followups):
        await run_one(state, at=START)
    [(reserved,)] = await rows(state, "select reserved_at from followups")
    assert reserved == stamp_us(START + timedelta(seconds=60))


async def test_duplicate_opens_and_users_make_one_request(jobs_state, server):
    state = jobs_state
    route = route_video(server, SEED, json=video_response(SEED, [server_item(200, channel=9)]))
    assert await state.followups.trigger("jan", video_id(SEED), START) == "scheduled"
    assert await state.followups.trigger("jan", video_id(SEED), START) == "skipped_duplicate"
    assert await state.followups.trigger("bob", video_id(SEED), START) == "skipped_duplicate"
    async with Worker(state.followups):
        await wait_idle(state.followups)
    assert route.call_count == 1
    # Both users get the related items.
    users = await rows(
        state, "select u.name from pool p join users u on u.id = p.user_id order by 1"
    )
    assert users == [("bob",), ("jan",)]


async def test_recently_fetched_failed_and_live_seeds_are_skipped(make_jobs, server):
    state = await make_jobs(followup_min_spacing_s=0, followup_per_hour=10)
    ok = route_video(server, SEED, json=video_response(SEED, []))
    missing = route_video(server, 2, status_code=404, json={"detail": "Video not found"})
    live = route_video(server, 3, json=video_response(3, [], liveNow=True, lengthSeconds=0))
    async with Worker(state.followups):
        assert await run_one(state, SEED) == "scheduled"
        assert await run_one(state, 2) == "scheduled"
        assert await run_one(state, 3) == "scheduled"
        # Fetched in the last 6 h, failed before, known live: no request.
        state.clock.advance(minutes=30)
        assert await run_one(state, SEED) == "skipped_recent"
        assert await run_one(state, 2) == "skipped_failed"
        assert await run_one(state, 3) == "skipped_recent"
        # After 7 h the good one may be fetched again; the failed and the live one never.
        state.clock.advance(hours=7)
        assert await run_one(state, SEED) == "scheduled"
        assert await run_one(state, 2) == "skipped_failed"
        assert await run_one(state, 3) == "skipped_live"
    assert (ok.call_count, missing.call_count, live.call_count) == (2, 1, 1)
    assert await rows(state, "select video_id, status from followups order by id") == [
        (video_id(SEED), "ok"),
        (video_id(2), "not_found"),
        (video_id(3), "ok"),
        (video_id(SEED), "ok"),
    ]


async def test_spacing_drops_a_followup_that_cannot_start_by_its_deadline(jobs_state, server):
    state = jobs_state
    first = route_video(server, 1, json=video_response(1, []))
    second = route_video(server, 2, json=video_response(2, []))
    before = jobs("dropped_spacing")
    await state.followups.trigger("jan", video_id(1), START)
    await state.followups.trigger("jan", video_id(2), START + timedelta(seconds=1))
    async with Worker(state.followups):
        await wait_idle(state.followups)
    assert (first.call_count, second.call_count) == (1, 0)
    assert jobs("dropped_spacing") == before + 1
    assert len(await rows(state, "select 1 from followups")) == 1  # a drop reserves nothing


async def test_a_followup_after_the_spacing_runs(jobs_state, server):
    state = jobs_state
    for n in (1, 2):
        route_video(server, n, json=video_response(n, []))
    async with Worker(state.followups):
        await run_one(state, 1)
        state.clock.advance(seconds=600)
        await run_one(state, 2)
    assert len(await rows(state, "select 1 from followups")) == 2


async def test_rolling_hourly_and_daily_budgets(make_jobs, server):
    state = await make_jobs(followup_min_spacing_s=0, followup_per_hour=2, followup_per_day=3)
    for n in range(1, 9):
        route_video(server, n, json=video_response(n, []))
    before_budget = jobs("dropped_budget")
    async with Worker(state.followups):
        for n in (1, 2, 3):
            await run_one(state, n)
        # Third within the hour: over the hourly limit, and not startable by its deadline.
        assert (await rows(state, "select count(*) from followups")) == [(2,)]
        assert jobs("dropped_budget") == before_budget + 1
        # An hour after the first reservation a slot frees up (rolling, not calendar).
        state.clock.advance(minutes=61)
        await run_one(state, 4)
        assert (await rows(state, "select count(*) from followups")) == [(3,)]
        # Daily limit of 3 reached; hourly would allow it.
        state.clock.advance(minutes=70)
        await run_one(state, 5)
        assert (await rows(state, "select count(*) from followups")) == [(3,)]
        assert jobs("dropped_budget") == before_budget + 2
        # A day after the first reservation the day budget frees up.
        state.clock.advance(hours=23)
        await run_one(state, 6)
        assert (await rows(state, "select count(*) from followups")) == [(4,)]
    left = await budget_left(state.db, state.settings, state.clock.now())
    assert (left["hour"], left["day"]) == (1, 2)


async def test_zero_budget_never_starts(make_jobs, server):
    state = await make_jobs(followup_per_hour=0)
    async with Worker(state.followups):
        await run_one(state, 1)
    assert len(server.calls) == 0


async def test_one_at_a_time_and_a_slow_one_makes_the_next_miss_its_deadline(make_jobs, server):
    state = await make_jobs(followup_min_spacing_s=0, followup_per_hour=10)

    def slow(_request):
        state.clock.advance(seconds=100)  # the server takes 100 s
        return httpx.Response(200, json=video_response(1, []))

    server.get(f"/api/v1/videos/{video_id(1)}").mock(side_effect=slow)
    second = route_video(server, 2, json=video_response(2, []))
    before = jobs("dropped_deadline")
    await state.followups.trigger("jan", video_id(1), START)
    await state.followups.trigger("jan", video_id(2), START + timedelta(seconds=5))
    async with Worker(state.followups):
        await wait_idle(state.followups)
    assert second.call_count == 0
    assert jobs("dropped_deadline") == before + 1


async def test_reservation_is_written_before_the_request_and_counts_on_failure(jobs_state, server):
    state = jobs_state
    seen = []

    async def check(request):
        seen.append(await rows(state, "select status from followups"))
        raise httpx.ConnectError("refused")

    server.get(f"/api/v1/videos/{video_id(1)}").mock(side_effect=check)
    async with Worker(state.followups):
        await run_one(state, 1)
    assert seen == [[("sent",)]]  # reserved and marked sent before the request left
    assert await rows(state, "select status from followups") == [("tripped",)]
    left = await budget_left(state.db, state.settings, state.clock.now())
    assert left["hour"] == 1 and left["day"] == 19  # a failed attempt used the budget


async def test_restart_does_not_replay_and_a_crashed_reservation_still_counts(jobs_state, server):
    state = jobs_state
    route = route_video(server, 1, json=video_response(1, []))
    # A crash after the reservation: the row is left 'sent'. A second video is merely pending.
    crashed = await state.db.followup_reserve(video_id(7), stamp(state.clock.now()))
    await state.db.followup_mark_sent(crashed, stamp(state.clock.now()))
    await state.followups.trigger("jan", video_id(1), state.clock.now())

    # The "restarted" process starts with no pending work and marks the unfinished row.
    from serendip.followups import Followups

    fresh = Followups(state.settings, state.db, state.breaker, state.clock, state.followups.client)
    async with Worker(fresh):
        await asyncio.sleep(0)
    assert route.call_count == 0
    assert await rows(state, "select video_id, status from followups") == [
        (video_id(7), "interrupted")
    ]
    left = await budget_left(state.db, state.settings, state.clock.now())
    assert left["hour"] == 1  # the crashed attempt counts
    assert left["spacing_ends_at"] is not None
    # It also counts as failed for that video: no second attempt.
    assert await fresh.trigger("jan", video_id(7), state.clock.now()) == "skipped_failed"


async def test_opens_before_this_process_are_never_followed_up(jobs_state, server):
    state = jobs_state
    await state.db.record_open(
        "jan", "tv", stamp(state.clock.now() - timedelta(minutes=2)), video_id(1), None
    )
    async with Worker(state.followups):
        await asyncio.sleep(0)
        assert state.followups.pending_count == 0
    assert len(server.calls) == 0


async def test_related_source_off_has_no_followups(make_jobs, server):
    state = await make_jobs(related_source="off")
    assert state.followups is None
    assert state.feed is not None  # subscriptions still refresh
    from serendip.events import IngestItem

    state.queue.put_nowait(
        IngestItem("open", "GET", f"/api/v1/videos/{video_id(1)}", {}, basic(), "", b"", START)
    )
    from conftest import drain

    await drain(state)
    assert await rows(state, "select count(*) from opens") == [(1,)]
    assert len(server.calls) == 0


async def test_consumer_triggers_followups_for_opens_only(jobs_state, server):
    from conftest import drain

    from serendip.events import IngestItem

    state = jobs_state
    for kind, path in (
        ("open", f"/api/v1/videos/{video_id(1)}"),
        ("search", "/api/v1/search"),
    ):
        state.queue.put_nowait(
            IngestItem(kind, "GET", path, {"q": "x"}, basic(), "", b"", state.clock.now())
        )
    state.queue.put_nowait(
        IngestItem(
            "open", "GET", f"/api/v1/videos/{video_id(2)}", {}, basic("stranger"), "", b"", START
        )
    )  # unknown credential: dropped, never followed up
    await drain(state)
    assert state.followups.pending_count == 1


async def test_queue_is_bounded(jobs_state):
    from serendip.followups import MAX_PENDING

    state = jobs_state
    outcomes = [
        await state.followups.trigger("jan", video_id(i), START) for i in range(MAX_PENDING + 3)
    ]
    assert outcomes.count("scheduled") == MAX_PENDING
    assert outcomes.count("dropped_queue_full") == 3


async def test_followup_budget_is_visible_in_status(tmp_path):
    from test_popular import client

    state = await make_state(tmp_path)
    try:
        async with client(state) as c:
            body = (await c.get("/status")).json()
        assert body["followup_budget_left"] == {"hour": 2, "day": 20, "spacing_ends_at": None}
        assert body["related_source"] == "followup"
        assert body["service_user_configured"] is False
        assert body["background_jobs"].startswith("disabled")
        assert body["breaker"] == {"open": False, "kind": None, "reason": None, "tripped_at": None}
        assert body["last_followup"] is None and body["last_feed"] == []
    finally:
        await state.db.close()


@pytest.mark.parametrize("related", [None, []])
async def test_missing_or_empty_related_list_is_a_plain_success(jobs_state, server, related):
    route_video(server, 1, json=video_response(1, related))
    async with Worker(jobs_state.followups):
        await run_one(jobs_state, 1)
    assert not await jobs_state.breaker.is_open()
    assert await rows(jobs_state, "select status, related_count from followups") == [("ok", 0)]


# --- admission, the send boundary and time accounting ------------------------------------------


async def test_concurrent_opens_of_one_video_are_admitted_once(jobs_state, server, monkeypatch):
    state = jobs_state
    route = route_video(server, SEED, json=video_response(SEED, [server_item(200, channel=9)]))
    gate = asyncio.Event()
    entered = []
    real_seen = state.db.followup_seen

    async def held_seen(vid):
        entered.append(vid)
        await gate.wait()  # every consumer that gets this far is held between check and enqueue
        return await real_seen(vid)

    monkeypatch.setattr(state.db, "followup_seen", held_seen)
    users = ["jan", "bob", "cy", "di"]
    admissions = [
        asyncio.create_task(state.followups.trigger(user, video_id(SEED), START)) for user in users
    ]
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(entered) == 1  # the other three wait for the admission lock
    gate.set()
    outcomes = await asyncio.gather(*admissions)
    assert sorted(outcomes) == [
        "scheduled",
        "skipped_duplicate",
        "skipped_duplicate",
        "skipped_duplicate",
    ]
    assert state.followups.pending_count == 1

    async with Worker(state.followups) as worker:
        await wait_idle(state.followups)
        assert not worker.task.done()  # the cleanup did not raise
    assert route.call_count == 1
    pooled = await rows(
        state, "select u.name from pool p join users u on u.id = p.user_id order by 1"
    )
    assert pooled == [("bob",), ("cy",), ("di",), ("jan",)]  # all four users share the job


async def test_cleanup_of_a_finished_job_tolerates_a_missing_id(jobs_state, server):
    state = jobs_state
    route_video(server, SEED, json=video_response(SEED, []))
    await state.followups.trigger("jan", video_id(SEED), START)
    state.followups._ids.clear()  # what a double admission used to leave behind
    async with Worker(state.followups) as worker:
        await asyncio.sleep(0.05)
        assert not worker.task.done()
    assert len(await rows(state, "select 1 from followups")) == 1


async def test_breaker_tripped_between_reservation_and_send_sends_nothing(
    jobs_state, server, monkeypatch
):
    state = jobs_state
    route = route_video(server, SEED, json=video_response(SEED, []))
    real_mark = state.db.followup_mark_sent

    async def trip_first(*args):
        await real_mark(*args)
        await state.breaker.trip("manual", "stopped from the CLI")  # after the reservation

    monkeypatch.setattr(state.db, "followup_mark_sent", trip_first)
    before = jobs("dropped_breaker")
    async with Worker(state.followups):
        await run_one(state)
    assert route.call_count == 0 and len(server.calls) == 0
    assert jobs("dropped_breaker") == before + 1
    assert await rows(state, "select status, outcome, sent_at from followups") == [
        ("skipped", "breaker", None)
    ]
    # The skipped reservation still counts in the budget ...
    left = await budget_left(state.db, state.settings, state.clock.now())
    assert left["hour"] == 1 and left["day"] == 19 and left["spacing_ends_at"] is not None
    # ... but asked the server for nothing, so the video is not marked failed or fetched.
    seen = await state.db.followup_seen(video_id(SEED))
    assert seen.failed_before is False and seen.reserved_last is None


async def test_deadline_passed_during_a_slow_reservation_drops_the_followup(
    jobs_state, server, monkeypatch
):
    state = jobs_state
    route = route_video(server, SEED, json=video_response(SEED, []))
    real_reserve = state.db.followup_reserve

    async def slow_reserve(*args):
        result = await real_reserve(*args)
        state.clock.advance(seconds=200)  # the write took longer than the deadline allows
        return result

    monkeypatch.setattr(state.db, "followup_reserve", slow_reserve)
    before = jobs("dropped_deadline")
    async with Worker(state.followups):
        await run_one(state)
    assert route.call_count == 0 and len(server.calls) == 0
    assert jobs("dropped_deadline") == before + 1
    assert await rows(state, "select status, outcome from followups") == [("skipped", "deadline")]
    assert (await budget_left(state.db, state.settings, state.clock.now()))["day"] == 19


async def test_reservations_keep_microseconds_so_a_budget_ends_exactly(make_jobs, server):
    state = await make_jobs(followup_per_hour=1, followup_min_spacing_s=0)
    route_video(server, SEED, json=video_response(SEED, []))
    received = START + timedelta(microseconds=900_000)
    async with Worker(state.followups):
        await run_one(state, at=received)
    reserved = received + timedelta(seconds=60)  # 10:01:00.9
    assert await rows(state, "select reserved_at, sent_at, finished_at from followups") == [
        (stamp_us(reserved), stamp_us(reserved), stamp_us(reserved))
    ]
    hour = timedelta(hours=1)
    just_before = await budget_left(
        state.db, state.settings, reserved + hour - timedelta(microseconds=1)
    )
    assert just_before["hour"] == 0  # whole seconds would have freed it 0.9 s early
    assert (await budget_left(state.db, state.settings, reserved + hour))["hour"] == 1


async def test_spacing_ends_at_the_exact_microsecond(jobs_state, server):
    state = jobs_state
    route_video(server, SEED, json=video_response(SEED, []))
    received = START + timedelta(microseconds=900_000)
    async with Worker(state.followups):
        await run_one(state, at=received)
    ends = received + timedelta(seconds=60 + 600)
    left = await budget_left(state.db, state.settings, ends - timedelta(microseconds=1))
    assert left["spacing_ends_at"] == stamp_us(ends)
    assert (await budget_left(state.db, state.settings, ends))["spacing_ends_at"] is None


@pytest.mark.parametrize("hours", [3, -3], ids=["forward", "backward"])
async def test_a_wall_clock_jump_does_not_release_the_spacing(jobs_state, server, hours):
    state = jobs_state
    first = route_video(server, 1, json=video_response(1, []))
    second = route_video(server, 2, json=video_response(2, []))
    async with Worker(state.followups):
        await run_one(state, 1)
        before = jobs("dropped_spacing")
        state.clock.jump_wall(hours=hours)  # the wall clock is set; no time passed
        await run_one(state, 2)
        assert jobs("dropped_spacing") == before + 1
    assert (first.call_count, second.call_count) == (1, 0)
    assert len(await rows(state, "select 1 from followups")) == 1


@pytest.mark.parametrize("hours", [2, -2], ids=["forward", "backward"])
async def test_a_wall_clock_jump_does_not_release_the_hourly_budget(make_jobs, server, hours):
    state = await make_jobs(followup_per_hour=1, followup_min_spacing_s=0)
    for n in (1, 2):
        route_video(server, n, json=video_response(n, []))
    async with Worker(state.followups):
        await run_one(state, 1)
        before = jobs("dropped_budget")
        state.clock.jump_wall(hours=hours)
        await run_one(state, 2)
        assert jobs("dropped_budget") == before + 1
    assert len(server.calls) == 1

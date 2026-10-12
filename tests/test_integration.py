"""Mirrored events -> feed job and follow-ups -> pool -> /popular, against a fake server."""

import json

import httpx
from conftest import (
    UA,
    Worker,
    basic,
    channel_id,
    drain,
    route_video,
    rows,
    server_item,
    video_id,
    video_response,
    wait_idle,
)
from test_popular import client as api_client

from serendip.ingest import create_ingest_app


def ingest_client(state) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_ingest_app(state))
    return httpx.AsyncClient(transport=transport, base_url="http://mirror.test")


async def test_mirrored_events_to_popular(jobs_state, server):
    state = jobs_state
    headers = {"Authorization": basic(), "User-Agent": UA}
    feed_items = [server_item(100 + i, channel=1 + i % 5) for i in range(40)]
    feed = server.post("/api/v1/feed").respond(
        json={"status": "ready", "videos": feed_items, "total": 40, "has_more": False}
    )
    related = [server_item(300 + i, channel=300 + i) for i in range(40)]
    seed_route = route_video(
        server,
        7,
        json=video_response(7, related, authorId=channel_id(1), extractionMethod="hybrid"),
    )

    # 1. The app's requests, mirrored.
    channels = [
        {"channel_id": channel_id(i), "site": "youtube", "channel_name": f"C{i}"}
        for i in range(1, 6)
    ]
    async with ingest_client(state) as mirror:
        await mirror.post(
            "/api/v1/feed", content=json.dumps({"channels": channels}), headers=headers
        )
        await mirror.get(f"/api/v1/videos/{video_id(7)}?proxy=true", headers=headers)
    await drain(state)
    assert state.followups.pending_count == 1

    # 2. The jobs: one feed post, one follow-up.
    await state.feed.run_due()
    async with Worker(state.followups):
        await wait_idle(state.followups)
    assert (feed.call_count, seed_route.call_count) == (1, 1)
    assert len(server.calls) == 2
    assert await rows(state, "select source, count(*) from pool group by 1 order by 1") == [
        ("adjacent", 40),
        ("subs", 40),
    ]

    # 3. /popular: only the pool is read.
    async with api_client(state) as c:
        first = (await c.get("/api/v1/popular", headers=headers)).json()
        state.clock.advance(seconds=20)
        second = (await c.get("/api/v1/popular", headers=headers)).json()
        status = (await c.get("/status")).json()
    assert len(server.calls) == 2  # no request-time HTTP
    assert len(first) == len(second) == 30
    assert not {i["videoId"] for i in first[:5]} & {i["videoId"] for i in second}
    assert video_id(7) not in {i["videoId"] for i in first + second}  # opened
    assert status["last_followup"]["status"] == "ok"
    assert status["last_followup"]["extraction_method"] == "hybrid"
    assert status["last_feed"][0]["status"] == "ready"
    assert status["pool"] == {"subs": 40, "related": 0, "adjacent": 40}
    assert status["followup_budget_left"]["hour"] == 1
    assert status["never_served_eligible"] == {"jan": 80 - 30 - 21}

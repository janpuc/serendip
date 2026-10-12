import asyncio
import json

import httpx
import pytest
import respx
from conftest import UA, basic, channel_id, drain, make_state

from serendip import consumer
from serendip.ingest import create_ingest_app

VIDEO = "dQw4w9WgXcQ"


def client(state) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_ingest_app(state))
    return httpx.AsyncClient(transport=transport, base_url="http://serendip.test")


async def rows(state, sql, *args):
    async with state.db._conn.execute(sql, args) as cur:
        return [tuple(r) for r in await cur.fetchall()]


HEADERS = {"Authorization": basic(), "User-Agent": UA}


async def test_open_event_records_only_proxy_flags(state):
    async with client(state) as c:
        url = f"/api/v1/videos/{VIDEO}?proxy=true&proxy_mode=download&sig=SECRET&token=x"
        assert (await c.get(url, headers=HEADERS)).status_code == 204
        assert (await c.get("/api/v1/videos/jNQXAC9IVRw", headers=HEADERS)).status_code == 204
        assert (await c.get(url, headers=HEADERS)).status_code == 204
    await drain(state)

    events = await rows(
        state, "select user_id, device, kind, video_id, query from events order by id"
    )
    assert events == [
        (1, "tv", "open", VIDEO, "proxy=true&proxy_mode=download"),
        (1, "tv", "open", "jNQXAC9IVRw", None),
        (1, "tv", "open", VIDEO, "proxy=true&proxy_mode=download"),
    ]
    assert await rows(state, "select video_id, count, channel_id from opens order by video_id") == [
        (VIDEO, 2, None),
        ("jNQXAC9IVRw", 1, None),
    ]
    assert await rows(state, "select id, name from users") == [(1, "jan")]
    assert "SECRET" not in json.dumps(await rows(state, "select * from events"))


async def test_feed_with_66_channels(state):
    channels = [
        {
            "channel_id": channel_id(n),
            "site": "youtube",
            "channel_name": f"Channel {n}",
            "channel_url": f"https://www.youtube.com/channel/{channel_id(n)}",
            "avatar_url": "https://yt3.ggpht.com/x?sig=abc",
        }
        for n in range(66)
    ]
    async with client(state) as c:
        resp = await c.post(
            "/api/v1/feed", json={"channels": channels, "limit": 50, "offset": 0}, headers=HEADERS
        )
    assert resp.status_code == 204
    await drain(state)

    assert await rows(state, "select count(*) from subscriptions") == [(66,)]
    assert await rows(
        state, "select name from subscriptions where channel_id = ?", channel_id(3)
    ) == [("Channel 3",)]
    assert await rows(state, "select kind, query from events") == [("subscriptions_seen", None)]


async def test_partial_and_repeated_feed_posts_only_add(state):

    def body(*ns, name=None):
        return {
            "channels": [
                {"channel_id": channel_id(n), "site": "youtube", "channel_name": name} for n in ns
            ]
        }

    async with client(state) as c:
        await c.post("/api/v1/feed", json=body(1, 2, 3, name="Old"), headers=HEADERS)
        await drain(state)
        first = await rows(state, "select channel_id, first_seen from subscriptions order by 1")
        await asyncio.sleep(1.1)
        await c.post("/api/v1/feed", json=body(3, 4), headers=HEADERS)
        await c.post("/api/v1/feed", json=body(3, 4), headers=HEADERS)
        await drain(state)

    ids = await rows(state, "select channel_id from subscriptions order by 1")
    assert ids == [(channel_id(n),) for n in (1, 2, 3, 4)]
    # first_seen is kept; last_seen_by_user moves only for channels in the later post.
    assert (
        await rows(
            state,
            "select channel_id, first_seen from subscriptions where channel_id < ?",
            channel_id(4),
        )
        == first
    )
    seen = dict(await rows(state, "select channel_id, last_seen_by_user from subscriptions"))
    assert seen[channel_id(1)] < seen[channel_id(3)] == seen[channel_id(4)]
    # A name-less repeat does not erase the name.
    assert await rows(
        state, "select name from subscriptions where channel_id = ?", channel_id(3)
    ) == [("Old",)]


async def test_feed_skips_non_youtube_and_bad_channel_ids(state):
    body = {
        "channels": [
            {"channel_id": channel_id(1), "site": "youtube"},
            {"channel_id": "someone", "site": "bilibili"},
            {"channel_id": channel_id(2), "site": "peertube"},
            {"channel_id": "UCshort", "site": "youtube"},
            "junk",
        ]
    }
    async with client(state) as c:
        await c.post("/api/v1/feed", json=body, headers=HEADERS)
    await drain(state)
    assert await rows(state, "select channel_id from subscriptions") == [(channel_id(1),)]


async def test_search_query_truncated_and_suggestions_ignored(state):
    async with client(state) as c:
        assert (
            await c.get("/api/v1/search", params={"q": "x" * 500}, headers=HEADERS)
        ).status_code == 204
        assert (
            await c.get("/api/v1/search/suggestions", params={"q": "abc"}, headers=HEADERS)
        ).status_code == 204
    await drain(state)
    assert await rows(state, "select kind, length(query) from events") == [("search", 200)]


async def test_invalid_ids_and_other_paths_are_ignored(state):
    async with client(state) as c:
        for path in (
            "/api/v1/videos/short",
            "/api/v1/videos/waytoolongvideoid",
            "/api/v1/videos/gSodHN1a3E!",
            f"/api/v1/videos/{VIDEO}/extra",
            "/api/v1/videos",
            "/api/v1/channels/UC0123456789012345678901",
            "/",
        ):
            assert (await c.get(path, headers=HEADERS)).status_code == 204
        assert (await c.post(f"/api/v1/videos/{VIDEO}", headers=HEADERS)).status_code == 204
    assert state.queue.qsize() == 0


async def test_malformed_json_dropped(state):
    async with client(state) as c:
        for content in (b"{not json", b'{"channels": "x"}', b"[]"):
            assert (
                await c.post("/api/v1/feed", content=content, headers=HEADERS)
            ).status_code == 204
    await drain(state)
    assert state.dropped["malformed_body"] == 3
    assert await rows(state, "select count(*) from events") == [(0,)]


async def test_oversized_body_dropped(tmp_path):
    state = await make_state(tmp_path, max_body_bytes=100)
    try:
        async with client(state) as c:
            resp = await c.post("/api/v1/feed", content=b"x" * 101, headers=HEADERS)
            assert resp.status_code == 204

            async def chunks():  # no Content-Length: the limit applies while streaming
                yield b"x" * 60
                yield b"x" * 60

            resp = await c.post("/api/v1/feed", content=chunks(), headers=HEADERS)
            assert resp.status_code == 204
        assert state.queue.qsize() == 0
        assert state.dropped["oversized"] == 2
    finally:
        await state.db.close()


async def test_full_queue_drops_and_still_204(tmp_path):
    state = await make_state(tmp_path, ingest_queue_max=2)
    try:
        async with client(state) as c:
            for _ in range(5):
                resp = await c.get(f"/api/v1/videos/{VIDEO}", headers=HEADERS)
                assert resp.status_code == 204
        assert state.queue.qsize() == 2
        assert state.dropped["queue_full"] == 3
    finally:
        await state.db.close()


async def test_any_host_accepted_and_device_mapping(state):
    async with client(state) as c:
        headers = {"Host": "yattee.janpuc.com-shadow", "Authorization": basic()}
        await c.get(
            f"/api/v1/videos/{VIDEO}", headers={**headers, "User-Agent": "x serendip-phone"}
        )
        await c.get(f"/api/v1/videos/{VIDEO}", headers={**headers, "User-Agent": "plain"})
    await drain(state)
    assert await rows(state, "select device from events order by id") == [("phone",), ("unknown",)]


@pytest.mark.parametrize(
    "auth, reason",
    [
        (None, "malformed_credential"),
        ("Basic !!!", "malformed_credential"),
        ("Bearer abc", "malformed_credential"),
        (basic("jan", "other-password"), "unknown_credential"),
        (basic("stranger"), "unknown_credential"),
    ],
)
@respx.mock(assert_all_called=False)
async def test_unrecognised_events_are_dropped_without_any_http_request(state, auth, reason):
    anything = respx.route().mock(return_value=httpx.Response(200))
    headers = {"Authorization": auth} if auth else {}
    async with client(state) as c:
        await c.get(f"/api/v1/videos/{VIDEO}", headers=headers)
    await drain(state)
    assert anything.call_count == 0
    assert state.dropped[reason] == 1
    assert await rows(state, "select count(*) from events") == [(0,)]


async def test_last_event_at_never_moves_backwards(state):
    from datetime import UTC, datetime

    from serendip.events import IngestItem

    def search(second: int) -> IngestItem:
        received = datetime(2026, 1, 1, 12, 0, second, tzinfo=UTC)
        return IngestItem("search", "GET", "/api/v1/search", {"q": "x"}, basic(), UA, b"", received)

    await consumer.process(state, search(30))
    await consumer.process(state, search(10))
    assert state.last_event_at == "2026-01-01T12:00:30Z"
    assert await state.db.last_event_at() == "2026-01-01T12:00:30Z"

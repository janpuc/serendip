from datetime import UTC, datetime, timedelta

import pytest
from conftest import channel_id, server_item, video_id

from serendip import catalog

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)


def test_parse_item_reads_a_server_item():
    r = catalog.parse_item(server_item(1, channel=2, viewCountText="1K views"), NOW)
    assert r is not None
    assert (r.video_id, r.author_id, r.length_s, r.title) == (
        video_id(1),
        channel_id(2),
        600,
        "Video 1",
    )
    assert r.published_est == NOW - timedelta(days=2)
    assert r.view_count_text == "1K views" and r.is_live is False and r.is_short is None


@pytest.mark.parametrize(
    "change",
    [
        {"videoId": "short"},
        {"videoId": None},
        {"title": ""},
        {"title": 5},
        {"author": None},
        {"authorId": ""},
        {"authorId": "notachannel"},
        {"lengthSeconds": None},
        {"lengthSeconds": "600"},
        {"lengthSeconds": -1},
        {"lengthSeconds": True},
    ],
)
def test_parse_item_rejects_what_the_app_would_reject(change):
    assert catalog.parse_item({**server_item(1), **change}, NOW) is None


def test_parse_item_rejects_non_dicts_and_missing_keys():
    for bad in (None, [], "x", 5):
        assert catalog.parse_item(bad, NOW) is None
    item = server_item(1)
    for key in ("videoId", "title", "author", "authorId", "lengthSeconds"):
        assert catalog.parse_item({k: v for k, v in item.items() if k != key}, NOW) is None


def test_related_items_get_an_estimated_upload_time_from_the_text():
    item = server_item(1, published=None, publishedText="3 weeks ago")
    r = catalog.parse_item(item, NOW)
    assert r.published_est == NOW - timedelta(weeks=3)
    assert catalog.parse_item(
        server_item(2, published=None, publishedText="Streamed 2 hours ago"), NOW
    ).published_est == NOW - timedelta(hours=2)
    assert (
        catalog.parse_item(
            server_item(3, published=None, publishedText="Premiered yesterday"), NOW
        ).published_est
        is None
    )
    assert catalog.estimate_published(None, NOW) is None


def test_live_upcoming_and_short_flags():
    r = catalog.parse_item(server_item(1, liveNow=True, isUpcoming=True, isShort=True), NOW)
    assert (r.is_live, r.is_upcoming, r.is_short) == (True, True, True)
    # The server hard-codes isShort false on related items; that is not knowledge.
    assert catalog.parse_item(server_item(2, isShort=False), NOW).is_short is None


def test_thumbnails_are_the_standard_set_for_the_video_id():
    thumbs = catalog.standard_thumbnails(video_id(1))
    assert [(t["quality"], t["width"], t["height"]) for t in thumbs] == [
        ("default", 120, 90), ("medium", 320, 180), ("high", 480, 360),
        ("sddefault", 640, 480), ("maxres", 1280, 720),
    ]  # fmt: skip
    assert thumbs[0]["url"] == f"https://i.ytimg.com/vi/{video_id(1)}/default.jpg"
    assert thumbs[4]["url"].endswith("/maxresdefault.jpg")


def _stored(**changes):
    values = {
        "video_id": video_id(1),
        "title": "t",
        "author": "a",
        "author_id": channel_id(1),
        "length_s": 600,
        "published_est": None,
        "published_text": None,
        "view_count": None,
        "view_count_text": None,
        "is_live": False,
        "is_upcoming": False,
        "is_short": None,
        "found_at": "2026-10-12T00:00:00Z",
    }
    return catalog.StoredVideo(**{**values, **changes})


@pytest.mark.parametrize(
    "changes, servable",
    [
        ({}, True),
        ({"length_s": 181}, True),
        ({"length_s": 180}, False),
        ({"length_s": 0}, False),
        ({"is_live": True}, False),
        ({"is_upcoming": True}, False),
        ({"is_short": True}, False),
        ({"author_id": ""}, False),
        ({"title": "  "}, False),
        ({"video_id": "bad"}, False),
    ],
)
def test_is_servable(changes, servable):
    assert catalog.is_servable(_stored(**changes), 180) is servable


def test_validate_list_item_requires_what_the_app_decodes():
    good = catalog.to_list_item(_stored())
    catalog.validate_list_item(good)
    for key in ("videoId", "title", "author", "authorId", "lengthSeconds"):
        broken = {k: v for k, v in good.items() if k != key}
        with pytest.raises(ValueError):
            catalog.validate_list_item(broken)
    for key, value in (("title", ""), ("lengthSeconds", 0), ("videoId", ""), ("authorId", "")):
        with pytest.raises(ValueError):
            catalog.validate_list_item({**good, key: value})
    with pytest.raises(ValueError):
        catalog.validate_list_item(
            {**good, "videoThumbnails": [{"url": "http://yattee.test/x.jpg"}]}
        )

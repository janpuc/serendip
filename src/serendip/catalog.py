"""Videos as serendip stores and serves them (DESIGN 5, 6 step 8).

Items arrive from Yattee Server's feed and related lists; they leave as the `VideoListItem` shape
of the server's /api/v1/trending. The app rejects a whole list for one item missing videoId,
title, author, authorId or lengthSeconds (YatteeServerAPI.swift:603-646), so both directions
validate item by item.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .events import CHANNEL_ID, VIDEO_ID

MAX_TITLE_CHARS = 500
MAX_AUTHOR_CHARS = 200
MAX_TEXT_CHARS = 100

# innertube/_converters.py:_STANDARD_THUMBNAILS. Built from the video id, never copied from the
# item: a URL in a server response can point at the internal server host.
_THUMBNAILS = [
    ("default.jpg", "default", 120, 90),
    ("mqdefault.jpg", "medium", 320, 180),
    ("hqdefault.jpg", "high", 480, 360),
    ("sddefault.jpg", "sddefault", 640, 480),
    ("maxresdefault.jpg", "maxres", 1280, 720),
]

_SECONDS_PER_UNIT = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2592000,
    "year": 31536000,
}
_RELATIVE = re.compile(
    r"(?:streamed\s+)?(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE
)


@dataclass(frozen=True)
class VideoRecord:
    video_id: str
    title: str
    author: str
    author_id: str
    length_s: int
    published_est: datetime | None
    published_text: str | None
    view_count: int | None
    view_count_text: str | None
    is_live: bool
    is_upcoming: bool
    is_short: bool | None  # True only when the source said so; False is not knowledge


def estimate_published(text: str | None, now: datetime) -> datetime | None:
    """Approximate upload time from '3 days ago' as converters/_formatting.py does."""
    match = _RELATIVE.search(text or "")
    if not match:
        return None
    return now - timedelta(seconds=int(match.group(1)) * _SECONDS_PER_UNIT[match.group(2).lower()])


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def parse_item(raw: object, now: datetime) -> VideoRecord | None:
    """A server item as a record, or None when it lacks a required field."""
    if not isinstance(raw, dict):
        return None
    video_id = raw.get("videoId")
    author_id = raw.get("authorId")
    title = _text(raw.get("title"), MAX_TITLE_CHARS)
    length = _int(raw.get("lengthSeconds"))
    author = raw.get("author")
    if (
        not isinstance(video_id, str)
        or not VIDEO_ID.match(video_id)
        or not isinstance(author_id, str)
        or not CHANNEL_ID.match(author_id)
        or title is None
        or length is None
        or not isinstance(author, str)
    ):
        return None
    published_text = _text(raw.get("publishedText"), MAX_TEXT_CHARS)
    published = _int(raw.get("published"))
    published_est = None
    if published:
        try:
            published_est = datetime.fromtimestamp(published, UTC)
        except (OverflowError, OSError, ValueError):
            published_est = None
    if published_est is None:
        published_est = estimate_published(published_text, now)
    return VideoRecord(
        video_id=video_id,
        title=title,
        author=author.strip()[:MAX_AUTHOR_CHARS],
        author_id=author_id,
        length_s=length,
        published_est=published_est,
        published_text=published_text,
        view_count=_int(raw.get("viewCount")),
        view_count_text=_text(raw.get("viewCountText"), MAX_TEXT_CHARS),
        is_live=raw.get("liveNow") is True,
        is_upcoming=raw.get("isUpcoming") is True,
        is_short=True if raw.get("isShort") is True else None,
    )


def standard_thumbnails(video_id: str) -> list[dict]:
    return [
        {
            "quality": quality,
            "url": f"https://i.ytimg.com/vi/{video_id}/{filename}",
            "width": width,
            "height": height,
        }
        for filename, quality, width, height in _THUMBNAILS
    ]


@dataclass(frozen=True)
class StoredVideo:
    """A videos row, as selection and the response use it."""

    video_id: str
    title: str
    author: str
    author_id: str
    length_s: int
    published_est: str | None
    published_text: str | None
    view_count: int | None
    view_count_text: str | None
    is_live: bool
    is_upcoming: bool
    is_short: bool | None
    found_at: str


def is_servable(video: StoredVideo, shorts_max_seconds: int) -> bool:
    """The hard constraint on single items (DESIGN 5 filters, 6.1 step 1)."""
    return (
        bool(VIDEO_ID.match(video.video_id))
        and bool(video.title.strip())
        and bool(video.author_id)
        and video.length_s > 0
        and not video.is_live
        and not video.is_upcoming
        and video.is_short is not True
        and video.length_s > shorts_max_seconds
    )


def to_list_item(video: StoredVideo) -> dict:
    """The server's VideoListItem, with exactly its camelCase keys."""
    published = None
    if video.published_est:
        published = int(datetime.fromisoformat(video.published_est).timestamp())
    return {
        "type": "video",
        "videoId": video.video_id,
        "title": video.title,
        "description": None,
        "author": video.author,
        "authorId": video.author_id,
        "authorUrl": f"/channel/{video.author_id}",
        "lengthSeconds": video.length_s,
        "published": published,
        "publishedText": video.published_text,
        "viewCount": video.view_count,
        "viewCountText": video.view_count_text,
        "likeCount": None,
        "videoThumbnails": standard_thumbnails(video.video_id),
        "liveNow": False,
        "isUpcoming": False,
        # Only items that passed the Shorts filter are served.
        "isShort": False,
    }


REQUIRED_STRINGS = ("videoId", "title", "author", "authorId")


def validate_list_item(item: dict) -> None:
    """Raises ValueError unless the item has what the app's decoder requires."""
    for key in REQUIRED_STRINGS:
        if not isinstance(item.get(key), str):
            raise ValueError(f"{key} missing")
    for key in ("videoId", "title", "authorId"):
        if not item[key]:
            raise ValueError(f"{key} empty")
    length = item.get("lengthSeconds")
    if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
        raise ValueError("lengthSeconds invalid")
    thumbs = item.get("videoThumbnails")
    if not isinstance(thumbs, list) or any(
        not isinstance(t, dict)
        or not isinstance(t.get("url"), str)
        or not t["url"].startswith("https://i.ytimg.com/")
        for t in thumbs
    ):
        raise ValueError("videoThumbnails invalid")

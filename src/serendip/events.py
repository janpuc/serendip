"""Turns a mirrored request into an event (DESIGN 4).

Only what the event needs is kept. In particular nothing from a video request's query string
other than the proxy flags: signed URLs and tokens can ride along there.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlencode

from .logs import scrub_urls

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_FLAG_VALUE = re.compile(r"^[A-Za-z0-9_]{1,16}$")

VIDEOS_PREFIX = "/api/v1/videos/"
FEED_PATH = "/api/v1/feed"
SEARCH_PATH = "/api/v1/search"

MAX_QUERY_CHARS = 200
MAX_CHANNEL_NAME_CHARS = 200

OPEN = "open"
SUBSCRIPTIONS_SEEN = "subscriptions_seen"
SEARCH = "search"


@dataclass(frozen=True)
class IngestItem:
    # Credentials and user data stay out of the repr, so a stray log line cannot print them.
    kind: str
    method: str
    path: str
    query: dict[str, str] = field(repr=False)  # selected values only, see selected_query
    authorization: str | None = field(repr=False)
    user_agent: str = field(repr=False)
    body: bytes = field(repr=False)
    received_at: datetime  # UTC


@dataclass
class ParsedEvent:
    kind: str
    video_id: str | None = None
    query: str | None = None
    channels: list[tuple[str, str | None]] = field(default_factory=list)


def classify(method: str, path: str) -> str | None:
    """The event kind a mirrored request can produce, or None if it is not one we handle."""
    if method == "GET" and path.startswith(VIDEOS_PREFIX):
        return OPEN if VIDEO_ID.match(path[len(VIDEOS_PREFIX) :]) else None
    if method == "POST" and path == FEED_PATH:
        return SUBSCRIPTIONS_SEEN
    if method == "GET" and path == SEARCH_PATH:
        return SEARCH
    return None


def clean_text(text: str, limit: int) -> str:
    """Free text as it may be stored: no URL query strings or signed parameters.

    Sanitized first, then truncated, so a cut cannot leave half a secret behind.
    """
    return scrub_urls(text.strip())[:limit].strip()


def selected_query(kind: str, params: dict[str, str]) -> dict[str, str]:
    if kind == OPEN:
        return {
            k: params[k] for k in ("proxy", "proxy_mode") if _FLAG_VALUE.match(params.get(k, ""))
        }
    if kind == SEARCH and "q" in params:
        return {"q": clean_text(params["q"], MAX_QUERY_CHARS)}
    return {}


def parse(item: IngestItem) -> ParsedEvent | str:
    """The event, or the drop reason."""
    if item.kind == OPEN:
        return ParsedEvent(
            OPEN,
            video_id=item.path[len(VIDEOS_PREFIX) :],
            query=urlencode(item.query) or None,
        )
    if item.kind == SEARCH:
        q = clean_text(item.query.get("q", ""), MAX_QUERY_CHARS)
        return ParsedEvent(SEARCH, query=q) if q else "empty"
    if item.kind == SUBSCRIPTIONS_SEEN:
        return _parse_feed(item.body)
    return "unhandled"


def _parse_feed(body: bytes) -> ParsedEvent | str:
    try:
        data = json.loads(body)
    except ValueError:
        return "malformed_body"
    channels = data.get("channels") if isinstance(data, dict) else None
    if not isinstance(channels, list):
        return "malformed_body"

    seen: dict[str, str | None] = {}
    for entry in channels:
        if not isinstance(entry, dict) or entry.get("site") != "youtube":
            continue
        channel_id = entry.get("channel_id")
        if not isinstance(channel_id, str) or not CHANNEL_ID.match(channel_id):
            continue
        name = entry.get("channel_name")
        name = clean_text(name, MAX_CHANNEL_NAME_CHARS) if isinstance(name, str) else None
        seen[channel_id] = name or seen.get(channel_id)
    if not seen:
        return "empty"
    return ParsedEvent(SUBSCRIPTIONS_SEEN, channels=list(seen.items()))

"""structlog setup. Credentials and signed URLs must never reach a log line.

The code does not log them in the first place; the processor below is the second lock.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

import structlog

REDACTED = "[redacted]"

_SECRET_KEYS = {"authorization", "proxy-authorization", "cookie", "password"}
_BASIC = re.compile(r"(?i)\b(basic|bearer)\s+\S+")
# Signed URLs carry their proof in the query string; drop the query of any URL or path.
_URL_QUERY = re.compile(r"((?:https?://)?[\w.:-]*/[^\s?\"']*)\?[^\s\"']*")
_SIGNED_PARAM = re.compile(r"(?i)\b(sig|signature|token|exp(?:ires?)?|hmac|key)=[^&\s\"']+")


def scrub(text: str) -> str:
    return scrub_urls(_BASIC.sub(REDACTED, text))


def scrub_urls(text: str) -> str:
    """Drops URL query strings and signed parameters; leaves ordinary words alone."""
    text = _URL_QUERY.sub(rf"\1?{REDACTED}", text)
    return _SIGNED_PARAM.sub(rf"\1={REDACTED}", text)


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {
            k: REDACTED if str(k).lower() in _SECRET_KEYS else _scrub_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_scrub_value(v) for v in value]
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(value)} bytes>"
    if value is None or isinstance(value, bool | int | float):
        return value
    # Anything else (a dataclass, a request) can carry credentials in its repr.
    return type(value).__name__


def redact(_logger: Any, _method: str, event_dict: dict) -> dict:
    return _scrub_value(event_dict)


def configure(level: str = "INFO", renderer: Callable | None = None) -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact,
            renderer or structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=False,
    )
    # httpx logs full request URLs at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)

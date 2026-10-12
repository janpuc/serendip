"""serendip's own calls to Yattee Server, as the service user (DESIGN 3, 5.1, 7).

Every call here goes through one gate (ServerClient.request) that checks the persistent breaker,
the caller's deadline and the clocks at the moment of sending, and that trips the breaker on a bad
reply while still holding the gate, so no other call can be sent between the reply and the trip.
The client follows no redirects (the Authorization header must stay with the one configured host),
reads at most MAX_RESPONSE_BYTES, has a hard total timeout, never retries, and never logs or stores
a header.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx
import structlog

from . import metrics
from .breaker import BOT_CHECK, ERROR, OPERATIONAL_STOP, Breaker
from .clock import Clock
from .config import Settings

log = structlog.get_logger()

MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# Specific phrases only: a video title can say "sign in" or "bot".
_BOT_TEXT = re.compile(
    r"sign[ _-]?in to confirm|confirm (?:that )?you.{1,8}re not a (?:bot|robot)"
    r"|not a (?:bot|robot)|login_required|sign in to continue|captcha|bot[ _-]?check"
    r"|unusual traffic",
    re.IGNORECASE,
)
OPERATIONAL_STATUSES = frozenset({401, 403, 429})

TIMEOUT = "timeout"
TRANSPORT = "transport"
MALFORMED = "malformed"
TOO_LARGE = "too_large"

# Why the gate refused to send.
SKIPPED_BREAKER = "breaker"
SKIPPED_DEADLINE = "deadline"
SKIPPED_CLOCK = "clock"
# UTC and monotonic time between two sends may differ by this much before nothing is sent.
MAX_CLOCK_DISAGREEMENT_S = 60.0


@dataclass
class Reply:
    """What came back. `data` is parsed JSON, only for a 200. `error` is set when no usable
    HTTP reply arrived; the body text is never kept beyond the bot-text test."""

    endpoint: str
    status: int | None = None
    data: object = None
    bot_text: bool = False
    error: str | None = None
    # Set when the gate refused to send; nothing went out and nothing was assessed.
    skipped: str | None = None
    seconds: float = 0.0
    trip: tuple[str, str] | None = field(default=None)  # (kind, reason) when the breaker must open

    @property
    def not_found(self) -> bool:
        return self.trip is None and self.status == 404

    @property
    def ok(self) -> bool:
        return self.trip is None and self.status == 200

    @property
    def status_label(self) -> str:
        return self.error or str(self.status)


def assess(reply: Reply) -> tuple[str, str] | None:
    """The breaker verdict for a reply: (kind, reason) to trip, or None (DESIGN 7)."""
    where = reply.endpoint
    if reply.bot_text:
        return BOT_CHECK, f"{where}: sign-in or bot text in a response (HTTP {reply.status})"
    if reply.error:
        return ERROR, f"{where}: {reply.error}"
    status = reply.status
    if status in OPERATIONAL_STATUSES:
        return OPERATIONAL_STOP, f"{where}: HTTP {status}"
    if status == 404 and where == "video":
        return None
    if status != 200:
        return ERROR, f"{where}: HTTP {status}"
    if reply.data is None:
        return ERROR, f"{where}: {MALFORMED}"
    return None


class ServerClient:
    def __init__(
        self,
        settings: Settings,
        breaker: Breaker,
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        authorization = settings.service_authorization()
        assert authorization is not None
        self._breaker = breaker
        self._clock = clock
        # One call at a time, from the final checks to the trip.
        self._gate = asyncio.Lock()
        self._last_send = (clock.monotonic(), clock.now())
        self._timeout_s = settings.server_timeout_s
        self._client = httpx.AsyncClient(
            base_url=settings.yattee_url,
            headers={"Authorization": authorization, "Accept": "application/json"},
            follow_redirects=False,
            timeout=httpx.Timeout(settings.server_timeout_s),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        endpoint: str,
        method: str,
        path: str,
        *,
        json_body: object = None,
        deadline: datetime | None = None,
        accept: Callable[[object], bool] | None = None,
    ) -> Reply:
        """Sends one call unless the breaker is open, `deadline` has passed or the clocks
        disagree, all checked at the moment of sending (`reply.skipped` says which). A 200 whose
        parsed body `accept` refuses counts as malformed. A reply that must trip the breaker
        trips it before the gate opens again."""
        reply = Reply(endpoint)
        async with self._gate:
            reply.skipped = await self._refusal(deadline)
            if reply.skipped:
                metrics.sends_skipped.labels(reply.skipped).inc()
                return reply
            started = time.monotonic()
            self._last_send = (self._clock.monotonic(), self._clock.now())
            try:
                async with asyncio.timeout(self._timeout_s):
                    await self._send(reply, method, path, json_body)
            except TimeoutError:
                reply.error = TIMEOUT
            except (httpx.HTTPError, OSError) as exc:
                reply.error = TRANSPORT
                # The type only: the text can carry the URL.
                log.warning("server_request_failed", endpoint=endpoint, error=type(exc).__name__)
            if (
                accept is not None
                and reply.status == 200
                and reply.error is None
                and reply.data is not None
                and not accept(reply.data)
            ):
                reply.error = MALFORMED
            reply.seconds = time.monotonic() - started
            reply.trip = assess(reply)
            metrics.server_requests.labels(endpoint, reply.status_label).inc()
            if reply.trip:
                await self._breaker.trip(*reply.trip)
        return reply

    async def _refusal(self, deadline: datetime | None) -> str | None:
        """The last checks before a send. Nothing after the breaker read may await."""
        if await self._breaker.is_open():
            return SKIPPED_BREAKER
        now = self._clock.now()
        if deadline is not None and now > deadline:
            return SKIPPED_DEADLINE
        last_mono, last_utc = self._last_send
        monotonic_s = self._clock.monotonic() - last_mono
        utc_s = (now - last_utc).total_seconds()
        if abs(utc_s - monotonic_s) > MAX_CLOCK_DISAGREEMENT_S:
            log.warning(
                "send_skipped_clock_disagreement",
                utc_elapsed_s=round(utc_s, 1),
                monotonic_elapsed_s=round(monotonic_s, 1),
            )
            return SKIPPED_CLOCK
        return None

    async def _send(self, reply: Reply, method: str, path: str, json_body: object) -> None:
        async with self._client.stream(method, path, json=json_body) as response:
            reply.status = response.status_code
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    reply.error = TOO_LARGE
                    return
                chunks.append(chunk)
        body = b"".join(chunks)
        reply.bot_text = bool(_BOT_TEXT.search(body.decode("utf-8", errors="replace")))
        if reply.status == 200:
            try:
                reply.data = json.loads(body)
            except ValueError:
                reply.error = MALFORMED

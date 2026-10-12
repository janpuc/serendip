"""The mirror endpoint. Every request gets 204 at once; the work happens in the consumer."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request, Response

from . import metrics
from .events import IngestItem, classify, selected_query
from .state import State

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def create_ingest_app(state: State) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    metrics.queue_depth.set_function(state.queue.qsize)

    @app.api_route("/{path:path}", methods=METHODS)
    async def mirror(request: Request, path: str) -> Response:
        full_path = "/" + path
        kind = classify(request.method, full_path)
        if kind is None:
            metrics.ingest_requests.labels("ignored", "ignored").inc()
            return Response(status_code=204)

        try:
            async with asyncio.timeout(state.settings.body_timeout_s):
                body = await _read_body(request, state.settings.max_body_bytes)
        except TimeoutError:
            _drop(state, kind, "body_timeout")
            return Response(status_code=204)
        if body is None:
            _drop(state, kind, "oversized")
            return Response(status_code=204)

        item = IngestItem(
            kind=kind,
            method=request.method,
            path=full_path,
            query=selected_query(kind, dict(request.query_params)),
            authorization=request.headers.get("authorization"),
            user_agent=request.headers.get("user-agent", ""),
            body=body,
            received_at=state.clock.now(),
        )
        try:
            state.queue.put_nowait(item)
        except asyncio.QueueFull:
            _drop(state, kind, "queue_full")
        else:
            metrics.ingest_requests.labels(kind, "queued").inc()
        return Response(status_code=204)

    return app


def _drop(state: State, kind: str, reason: str) -> None:
    metrics.ingest_requests.labels(kind, "dropped").inc()
    state.drop(reason)


async def _read_body(request: Request, limit: int) -> bytes | None:
    """The body, or None if it is larger than limit. Stops reading as soon as it is."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)

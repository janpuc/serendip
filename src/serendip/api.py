"""Port 8080: health, status and metrics. /api/v1/popular arrives in stage (b)."""

from __future__ import annotations

from fastapi import FastAPI, Response

from . import metrics
from .state import State


def create_api_app(state: State) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health(response: Response) -> dict:
        if not state.healthy:
            response.status_code = 503
        return {"ok": state.healthy}

    @app.get("/status")
    async def status() -> dict:
        counts = await state.db.counts()
        return {
            "version": state.settings.version,
            "queue_depth": state.queue.qsize(),
            "dropped": dict(state.dropped),
            "counts": counts,
            "last_event_at": state.last_event_at or await state.db.last_event_at(),
            "users_configured": state.auth.user_count,
        }

    @app.get("/metrics")
    async def prometheus() -> Response:
        return Response(metrics.render(), media_type=metrics.CONTENT_TYPE)

    return app

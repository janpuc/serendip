"""Port 8080: /api/v1/popular, health, status and metrics."""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import lists, metrics
from .followups import budget_left
from .state import State

log = structlog.get_logger()

NO_STORE = {"Cache-Control": "private, no-store"}
# As Yattee Server sends it, so the app treats the answer like the server's own.
UNAUTHORIZED = {**NO_STORE, "WWW-Authenticate": 'Basic realm="Yattee Server"'}


def create_api_app(state: State) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/api/v1/popular")
    async def popular(request: Request) -> Response:
        verdict = state.auth.check(request.headers.get("authorization"))
        if verdict.name is None:
            return JSONResponse({"detail": "Not authenticated"}, 401, headers=UNAUTHORIZED)
        device = state.settings.device_for(request.headers.get("user-agent", ""))
        try:
            served = await lists.serve_list(
                state.db, state.settings, verdict.name, device, state.clock.now()
            )
        except Exception as exc:
            # Nothing was recorded; the type only, as in the consumer.
            log.error("popular_failed", error=type(exc).__name__)
            return JSONResponse({"detail": "Internal Server Error"}, 500, headers=NO_STORE)
        for kind, count in served.kinds.items():
            metrics.list_items.labels(kind).inc(count)
        return JSONResponse(served.items, headers=NO_STORE)

    @app.get("/health")
    async def health(response: Response) -> dict:
        if not state.healthy:
            response.status_code = 503
        return {"ok": state.healthy}

    @app.get("/status")
    async def status() -> dict:
        now = state.clock.now()
        settings = state.settings
        counts = await state.db.counts()
        breaker = await state.breaker.state()
        return {
            "version": settings.version,
            "queue_depth": state.queue.qsize(),
            "dropped": dict(state.dropped),
            "counts": counts,
            "last_event_at": state.last_event_at or await state.db.last_event_at(),
            "users_configured": state.auth.user_count,
            "breaker": {
                "open": breaker.open,
                "kind": breaker.kind,
                "reason": breaker.reason,
                "tripped_at": breaker.tripped_at,
            },
            "related_source": settings.related_source,
            "service_user_configured": settings.service_user_configured,
            "background_jobs": (
                "enabled" if settings.service_user_configured else "disabled: no service user"
            ),
            "pool": await state.db.pool_counts(now),
            "never_served_eligible": {
                name: await lists.never_served_eligible(state.db, settings, name, now)
                for name in await state.db.user_names()
            },
            "last_feed": await state.db.last_feed_runs(),
            "last_followup": await state.db.last_followup(),
            "followups_pending": state.followups.pending_count if state.followups else 0,
            "followup_budget_left": await budget_left(
                state.db,
                settings,
                now,
                state.followups.in_process_reservations(now) if state.followups else None,
            ),
        }

    @app.get("/metrics")
    async def prometheus() -> Response:
        await state.breaker.state()  # the CLI can reset it from another process
        return Response(metrics.render(), media_type=metrics.CONTENT_TYPE)

    return app

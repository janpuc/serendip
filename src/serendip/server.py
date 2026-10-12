"""One process: the ingest server, the API server, the consumer and the housekeeping loops."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Callable

import structlog
import uvicorn

from . import consumer
from .api import create_api_app
from .auth import Authenticator
from .config import Settings
from .db import Database
from .ingest import create_ingest_app
from .state import State

log = structlog.get_logger()

CHECKPOINT_EVERY_S = 5 * 60
RETENTION_EVERY_S = 24 * 60 * 60
# Beyond this many open connections uvicorn answers 503; the mirror discards the response.
INGEST_MAX_CONNECTIONS = 256
# Open connections get this long to finish after a stop signal.
GRACEFUL_SHUTDOWN_S = 10


class _Server(uvicorn.Server):
    """Signals are handled by serve(), once for both servers."""

    def capture_signals(self):
        return contextlib.nullcontext()


def _server(app, port: int, **kwargs) -> _Server:
    # Access logs would print request paths and query strings.
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
        **kwargs,
    )
    return _Server(config)


async def _every(seconds: float, job, *, first_run: bool = False) -> None:
    if not first_run:
        await asyncio.sleep(seconds)
    while True:
        try:
            await job()
        except Exception as exc:
            log.warning("housekeeping_failed", job=job.__name__, error=type(exc).__name__)
        await asyncio.sleep(seconds)


def supervise(task: asyncio.Task, state: State, stop: Callable[[], None]) -> None:
    """A critical task must run until shutdown. If it ends or raises before, the process is
    unhealthy: /health answers 503 and both servers stop, so the process exits non-zero and
    Kubernetes restarts it."""

    def ended(done: asyncio.Task) -> None:
        if state.stopping:
            return
        if done.cancelled():
            error = "cancelled"
        else:
            exc = done.exception()
            error = "returned" if exc is None else type(exc).__name__
        log.error("background_task_ended", task=done.get_name(), error=error)
        state.healthy = False
        stop()

    task.add_done_callback(ended)


async def serve(settings: Settings) -> int:
    """Runs until stopped; returns the process exit code."""
    db = await Database.connect(settings.db_path)
    auth = Authenticator(settings)
    state = State(settings, db, auth, asyncio.Queue(maxsize=settings.ingest_queue_max))

    ingest = _server(
        create_ingest_app(state), settings.ingest_port, limit_concurrency=INGEST_MAX_CONNECTIONS
    )
    api = _server(create_api_app(state), settings.api_port)

    def stop() -> None:
        # Set first, so the servers and tasks ending because of this stop are not taken for a
        # failure by supervise().
        state.stopping = True
        ingest.should_exit = True
        api.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)

    async def retention() -> None:
        deleted = await db.apply_retention()
        log.info("retention", events_deleted=deleted)

    background = [
        asyncio.create_task(consumer.run(state), name="consumer"),
        asyncio.create_task(_every(CHECKPOINT_EVERY_S, db.checkpoint), name="checkpoint"),
        asyncio.create_task(_every(RETENTION_EVERY_S, retention, first_run=True), name="retention"),
    ]
    # A server that ends on its own (it cannot bind its port, say) stops the other one too.
    servers = [
        asyncio.create_task(ingest.serve(), name="ingest_server"),
        asyncio.create_task(api.serve(), name="api_server"),
    ]
    for task in background + servers:
        supervise(task, state, stop)
    log.info("serving", api_port=settings.api_port, ingest_port=settings.ingest_port)
    try:
        await asyncio.gather(*servers, return_exceptions=True)
    finally:
        state.stopping = True
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await db.checkpoint()
        await db.close()
    return 0 if state.healthy else 1

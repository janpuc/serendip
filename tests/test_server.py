import asyncio

import httpx

from serendip import consumer, server
from serendip.api import create_api_app


async def health_status(state) -> int:
    transport = httpx.ASGITransport(app=create_api_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://serendip.test") as c:
        return (await c.get("/health")).status_code


async def test_crashed_consumer_flips_health_and_stops_servers(state, monkeypatch):
    async def broken(_state):
        raise RuntimeError("boom")

    monkeypatch.setattr(consumer, "run", broken)
    stopped: list[bool] = []
    assert await health_status(state) == 200

    task = asyncio.create_task(consumer.run(state), name="consumer")
    server.supervise(task, state, lambda: stopped.append(True))
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert stopped == [True]
    assert state.healthy is False
    assert await health_status(state) == 503


async def test_task_that_returns_is_also_fatal(state):
    stopped: list[bool] = []

    async def returns():
        return None

    task = asyncio.create_task(returns(), name="retention")
    server.supervise(task, state, lambda: stopped.append(True))
    await task
    await asyncio.sleep(0)
    assert stopped == [True]
    assert state.healthy is False


async def test_cancellation_during_shutdown_is_not_a_failure(state):
    stopped: list[bool] = []
    task = asyncio.create_task(asyncio.sleep(30), name="consumer")
    server.supervise(task, state, lambda: stopped.append(True))
    state.stopping = True
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert stopped == []
    assert state.healthy is True


def test_servers_have_graceful_shutdown_timeout(state):
    srv = server._server(create_api_app(state), 8080)
    assert srv.config.timeout_graceful_shutdown == 10

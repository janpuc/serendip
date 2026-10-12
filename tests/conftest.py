import asyncio
import base64
import hashlib
from pathlib import Path

import pytest

from serendip import consumer, migrate
from serendip.auth import Authenticator
from serendip.config import Settings
from serendip.db import Database
from serendip.state import State

ROOT = Path(__file__).parent.parent
UA = "Mozilla/5.0 (Macintosh) Safari/605.1.15 serendip-tv"


def basic(user: str = "jan", password: str = "pw") -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def sha256_hex(header: str) -> str:
    return hashlib.sha256(header.encode()).hexdigest()


def channel_id(n: int) -> str:
    return f"UC{n:022d}"


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "db_path": str(tmp_path / "serendip.db"),
        "device_ua_map": "serendip-tv=tv,serendip-phone=phone",
        # The default test credential belongs to the one configured user.
        "users": f"jan={sha256_hex(basic())}",
    }
    return Settings(_env_file=None, **{**values, **overrides})


async def make_state(tmp_path, **overrides) -> State:
    settings = make_settings(tmp_path, **overrides)
    migrate.migrate(settings.db_path)
    db = await Database.connect(settings.db_path)
    auth = Authenticator(settings)
    return State(settings, db, auth, asyncio.Queue(maxsize=settings.ingest_queue_max))


async def drain(state: State) -> None:
    """Runs the consumer until the queue is empty."""
    task = asyncio.create_task(consumer.run(state))
    try:
        await asyncio.wait_for(state.queue.join(), 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
async def state(tmp_path):
    st = await make_state(tmp_path)
    yield st
    await st.db.close()

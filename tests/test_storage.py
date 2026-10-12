import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import structlog
from conftest import channel_id, make_settings, sha256_hex

from serendip import cli, logs, migrate
from serendip.db import Database, stamp


async def test_migrations_are_idempotent(tmp_path):
    path = str(tmp_path / "m.db")
    assert migrate.migrate(path) == ["001_observe.sql", "002_discover.sql"]
    assert migrate.migrate(path) == []
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("select name from sqlite_master where type = 'table'")}
    assert {"users", "events", "subscriptions", "opens", "schema_version"} <= tables
    assert {
        "videos",
        "pool",
        "lists",
        "impressions",
        "exposures",
        "followups",
        "feed_runs",
        "breaker",
    } <= tables
    assert conn.execute("select version, name from schema_version").fetchall() == [
        (1, "001_observe.sql"),
        (2, "002_discover.sql"),
    ]
    assert conn.execute("pragma journal_mode").fetchone() == ("wal",)
    conn.close()


async def test_retention_deletes_only_old_events(state):
    now = datetime.now(UTC)
    old = stamp(now - timedelta(days=181))
    recent = stamp(now - timedelta(days=179))
    await state.db.record_search("jan", "tv", old, "old")
    await state.db.record_search("jan", "tv", recent, "recent")
    assert await state.db.apply_retention() == 1
    async with state.db._conn.execute("select query from events") as cur:
        assert [r[0] for r in await cur.fetchall()] == ["recent"]


def test_subs_reset_clears_only_that_user(tmp_path, monkeypatch, capsys):
    settings = make_settings(tmp_path)
    migrate.migrate(settings.db_path)

    async def seed():
        db = await Database.connect(settings.db_path)
        ts = stamp(datetime.now(UTC))
        await db.record_subscriptions("a", "tv", ts, [(channel_id(1), "A"), (channel_id(2), None)])
        await db.record_subscriptions("b", "tv", ts, [(channel_id(1), "A")])
        await db.close()

    asyncio.run(seed())

    monkeypatch.setenv("SERENDIP_DB_PATH", settings.db_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["subs", "reset", "--user", "a"]) == 0
    assert "removed 2" in capsys.readouterr().out

    conn = sqlite3.connect(settings.db_path)
    assert conn.execute(
        "select users.name, count(*) from subscriptions join users on users.id = user_id group by 1"
    ).fetchall() == [("b", 1)]
    conn.close()


def test_cli_migrate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERENDIP_DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["migrate"]) == 0
    out = capsys.readouterr().out
    assert "applied 001_observe.sql" in out
    assert "applied 002_discover.sql" in out


def test_device_map():
    s = make_settings(Path("/tmp"), device_ua_map="serendip-tv=tv, serendip-phone=phone")
    assert s.device_for("Mozilla Serendip-TV") == "tv"
    assert s.device_for("x serendip-phone") == "phone"
    assert s.device_for("Mozilla") == "unknown"
    assert make_settings(Path("/tmp"), device_ua_map="").device_for("x") == "unknown"


def test_redaction_processor():
    out = logs.redact(
        None,
        "info",
        {
            "event": "got Basic dXNlcjpwdw== and https://rr1.googlevideo.com/v?sig=AbC&x=1",
            "authorization": "Basic dXNlcjpwdw==",
            "nested": {"Authorization": "Basic zzz", "u": "/api/v1/videos/x?token=1&sig=2"},
        },
    )
    text = str(out)
    for forbidden in ("Basic ", "sig=AbC", "dXNlcjpwdw", "token=1", "sig=2", "x=1"):
        assert forbidden not in text


async def test_logs_from_a_full_run_contain_no_credentials(state):
    from conftest import basic, drain

    from serendip.events import IngestItem

    lines: list[str] = []
    logs.configure("DEBUG", renderer=lambda _l, _m, ed: lines.append(str(ed)) or "")
    try:
        structlog.get_logger().info(
            "probe", authorization=basic(), url="https://h/videoplayback?sig=abc&expire=1"
        )
        for name, path in (
            ("jan", "/api/v1/videos/dQw4w9WgXcQ"),
            ("other", "/api/v1/videos/jNQXAC9IVRw"),
        ):
            state.queue.put_nowait(
                IngestItem(
                    "open",
                    "GET",
                    path,
                    {"proxy": "true"},
                    basic(name),
                    "",
                    b"",
                    datetime.now(UTC),
                )
            )
        await drain(state)
    finally:
        logs.configure("INFO")
    assert lines
    joined = "\n".join(lines)
    assert "Basic " not in joined
    assert sha256_hex(basic()) not in joined
    assert "sig=" not in joined


def _item(**overrides):
    from serendip.events import IngestItem

    values = {
        "kind": "feed",
        "method": "POST",
        "path": "/api/v1/feed",
        "query": {"q": "https://h/v?sig=SECRET"},
        "authorization": "Basic dXNlcjpwdw==",
        "user_agent": "UASECRET",
        "body": b"BODYSECRET",
        "received_at": datetime.now(UTC),
    }
    return IngestItem(**{**values, **overrides})


def test_ingest_item_repr_hides_credentials_and_user_data():
    text = repr(_item())
    for forbidden in ("Basic", "dXNlcjpwdw", "BODYSECRET", "SECRET", "UASECRET", "body", "query"):
        assert forbidden not in text
    assert "/api/v1/feed" in text


def test_redaction_handles_bytes_objects_and_standalone_exp():
    out = logs.redact(
        None,
        "info",
        {
            "event": "https://h/v?exp=123 and exp=9&x=1",
            "raw": b"BODYSECRET",
            "item": _item(),
            "nested": [b"abc", {"o": object()}],
            "n": 3,
        },
    )
    assert out["raw"] == "<10 bytes>"
    assert out["item"] == "IngestItem"
    assert out["nested"] == ["<3 bytes>", {"o": "object"}]
    assert out["n"] == 3
    assert "exp=123" not in out["event"] and "exp=9" not in out["event"]


def test_structlog_never_prints_an_item_or_bytes():
    lines: list[str] = []
    logs.configure("DEBUG", renderer=lambda _l, _m, ed: lines.append(str(ed)) or "")
    try:
        structlog.get_logger().info("probe", item=_item(), body=b"BODYSECRET")
    finally:
        logs.configure("INFO")
    joined = "\n".join(lines)
    for forbidden in ("Basic", "dXNlcjpwdw", "BODYSECRET", "SECRET"):
        assert forbidden not in joined


async def test_failed_open_leaves_no_event_row(state, monkeypatch):
    conn = state.db._conn
    real_execute = conn.execute

    async def failing(sql, *args):
        if sql.startswith("insert into opens"):
            raise RuntimeError("boom")
        return await real_execute(sql, *args)

    monkeypatch.setattr(conn, "execute", failing)
    with pytest.raises(RuntimeError):
        await state.db.record_open("jan", "tv", "2026-10-12T09:00:00Z", "dQw4w9WgXcQ", None)
    monkeypatch.undo()

    await state.db.record_search("jan", "tv", "2026-10-12T09:01:00Z", "ok")
    async with conn.execute("select kind from events") as cur:
        assert [r[0] for r in await cur.fetchall()] == ["search"]


async def test_cancelled_write_rolls_back(state, monkeypatch):
    conn = state.db._conn
    real_execute = conn.execute

    async def hang(sql, *args):
        if sql.startswith("insert into opens"):
            await asyncio.sleep(30)
        return await real_execute(sql, *args)

    monkeypatch.setattr(conn, "execute", hang)
    task = asyncio.create_task(
        state.db.record_open("jan", "tv", "2026-10-12T09:00:00Z", "dQw4w9WgXcQ", None)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()

    await state.db.record_search("jan", "tv", "2026-10-12T09:01:00Z", "ok")
    async with conn.execute("select kind from events") as cur:
        assert [r[0] for r in await cur.fetchall()] == ["search"]


async def test_out_of_order_events_never_move_time_backwards(state):
    db = state.db
    newer, older = "2026-10-12T10:00:00Z", "2026-10-12T08:00:00Z"
    video, cid = "dQw4w9WgXcQ", channel_id(1)
    await db.record_open("jan", "tv", newer, video, None)
    await db.record_open("jan", "tv", older, video, None)
    await db.record_subscriptions("jan", "tv", newer, [(cid, "New name")])
    await db.record_subscriptions("jan", "tv", older, [(cid, "Old name")])

    async def one(sql):
        async with db._conn.execute(sql) as cur:
            return tuple(await cur.fetchone())

    assert await one("select last_open_utc, count from opens") == (newer, 2)
    assert await one("select first_seen, last_seen_by_user, name from subscriptions") == (
        older,
        newer,
        "New name",
    )
    assert await one("select first_seen from users") == (older,)

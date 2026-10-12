from datetime import UTC, datetime

import httpx
import pytest
import respx
from conftest import basic, drain, make_settings, make_state, sha256_hex

from serendip import metrics
from serendip.auth import MALFORMED, UNKNOWN_CREDENTIAL, USER, Authenticator
from serendip.events import IngestItem


def auth_checks(result: str) -> float:
    return metrics.REGISTRY.get_sample_value("serendip_auth_checks_total", {"result": result}) or 0


def search(header: str) -> IngestItem:
    return IngestItem(
        "search", "GET", "/api/v1/search", {"q": "x"}, header, "", b"", datetime.now(UTC)
    )


def test_configured_user_is_recognised(tmp_path):
    before = auth_checks("user")
    verdict = Authenticator(make_settings(tmp_path)).check(basic())
    assert (verdict.result, verdict.name) == (USER, "jan")
    assert auth_checks("user") == before + 1


def test_scheme_is_case_insensitive_but_the_hash_covers_the_whole_value(tmp_path):
    auth = Authenticator(make_settings(tmp_path))
    assert auth.check(basic().replace("Basic", "basic")).result == UNKNOWN_CREDENTIAL


@pytest.mark.parametrize(
    "header",
    [basic("jan", "other"), basic("stranger"), basic("jan") + " "],
)
def test_unlisted_header_is_unknown_credential(tmp_path, header):
    before = auth_checks("unknown_credential")
    verdict = Authenticator(make_settings(tmp_path)).check(header)
    assert (verdict.result, verdict.name) == (UNKNOWN_CREDENTIAL, None)
    assert auth_checks("unknown_credential") == before + 1


@pytest.mark.parametrize(
    "header",
    [None, "", "Basic", "Basic !!!", "Bearer abc", "Basic Zm9v", "Basic OnB3"],  # no ':'; no user
)
def test_malformed_header_is_rejected(tmp_path, header):
    before = auth_checks("malformed")
    verdict = Authenticator(make_settings(tmp_path)).check(header)
    assert (verdict.result, verdict.name) == (MALFORMED, None)
    assert auth_checks("malformed") == before + 1


def test_no_users_configured_recognises_nobody(tmp_path):
    auth = Authenticator(make_settings(tmp_path, users=""))
    assert auth.user_count == 0
    assert auth.check(basic()).result == UNKNOWN_CREDENTIAL


@respx.mock(assert_all_called=False)
async def test_unknown_credential_never_causes_an_http_request(tmp_path):
    anything = respx.route().mock(return_value=httpx.Response(200))
    state = await make_state(tmp_path)
    try:
        await consumer_process(state, basic("stranger"))
        assert anything.call_count == 0
        assert state.dropped["unknown_credential"] == 1
    finally:
        await state.db.close()


async def consumer_process(state, header):
    from serendip import consumer

    await consumer.process(state, search(header))


async def test_users_are_keyed_by_configured_name_not_submitted_name(tmp_path):
    state = await make_state(
        tmp_path,
        users=f"jan={sha256_hex(basic('jan-phone'))},bob={sha256_hex(basic('jan-tv'))}",
    )
    try:
        for header in (basic("jan-phone"), basic("jan-phone", "x"), basic("jan-tv")):
            state.queue.put_nowait(search(header))
        await drain(state)
        async with state.db._conn.execute(
            "select users.name, count(*) from events join users on users.id = user_id"
            " group by 1 order by 1"
        ) as cur:
            assert [tuple(r) for r in await cur.fetchall()] == [("bob", 1), ("jan", 1)]
        async with state.db._conn.execute("select name from users order by name") as cur:
            assert [tuple(r) for r in await cur.fetchall()] == [("bob",), ("jan",)]
        assert state.dropped["unknown_credential"] == 1
    finally:
        await state.db.close()


async def test_status_reports_configured_user_count_and_no_secrets(tmp_path):
    from serendip.api import create_api_app

    for users, expected in (("", 0), (f"jan={sha256_hex(basic())}", 1)):
        state = await make_state(tmp_path, users=users)
        try:
            transport = httpx.ASGITransport(app=create_api_app(state))
            async with httpx.AsyncClient(transport=transport, base_url="http://s.test") as c:
                body = (await c.get("/status")).json()
            assert body["users_configured"] == expected
            assert sha256_hex(basic()) not in str(body)
        finally:
            await state.db.close()

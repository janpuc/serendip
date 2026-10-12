import pytest
from conftest import basic, make_settings
from pydantic import ValidationError

from serendip.config import Settings


@pytest.mark.parametrize(
    "name, value",
    [
        ("api_port", 0),
        ("api_port", 65536),
        ("ingest_port", -1),
        ("ingest_port", 70000),
        ("ingest_queue_max", 0),
        ("max_body_bytes", 0),
        ("body_timeout_s", 0),
        ("users", "jan"),
        ("users", f"jan={'A' * 64}"),
        ("users", f"jan={'a' * 63}"),
        ("users", f"jan={'a' * 64},bob=nothex"),
        ("users", f"bad name={'a' * 64}"),
        ("users", "=" + "a" * 64),
        ("users", f"{'n' * 65}={'a' * 64}"),
        ("users", f"jan={'a' * 64},jan={'b' * 64}"),
        ("users", f"jan={'a' * 64},bob={'a' * 64}"),
        ("related_source", "next"),
        ("related_source", "anything"),
        ("carry_max", 0.51),
        ("carry_max", -0.1),
        ("list_size", 0),
        ("head_size", -1),
        ("repeat_window_s", -1),
        ("followup_deadline_s", 0),
        ("followup_per_hour", -1),
        ("feed_interval_s", 0),
        ("feed_limit", 1001),
        ("feed_limit", 0),
        ("timezone", "Mars/Olympus"),
        ("yattee_username", "a:b"),
        ("server_timeout_s", 0),
    ],
)
def test_invalid_settings_fail_to_load(tmp_path, name, value):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, **{name: value})


def test_invalid_environment_fails_to_load(monkeypatch):
    monkeypatch.setenv("SERENDIP_API_PORT", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_users_are_parsed(tmp_path):
    one, two = "a" * 64, "b" * 64
    settings = make_settings(tmp_path, users=f"jan.p-1_x={one}, bob={two},")
    assert settings.user_names_by_hash() == {one: "jan.p-1_x", two: "bob"}
    assert Settings(_env_file=None).user_names_by_hash() == {}


def test_next_is_refused_with_a_clear_error(monkeypatch):
    monkeypatch.setenv("SERENDIP_RELATED_SOURCE", "next")
    with pytest.raises(ValidationError, match="reserved for a later stage"):
        Settings(_env_file=None)


def test_stage_b_defaults(tmp_path):
    s = make_settings(tmp_path)
    assert (s.related_source, s.list_size, s.head_size, s.carry_max) == ("followup", 30, 5, 0.3)
    assert (s.repeat_window_s, s.followup_delay_s, s.followup_deadline_s) == (15, 60, 120)
    assert (s.followup_per_hour, s.followup_per_day, s.followup_min_spacing_s) == (2, 20, 600)
    assert (s.feed_interval_s, s.feed_limit, s.shorts_max_seconds) == (21600, 1000, 180)
    assert s.timezone == "Europe/Warsaw"
    assert s.service_user_configured is False
    assert s.breaker_start_open is False
    assert s.service_authorization() is None
    assert make_settings(tmp_path, related_source="off").related_source == "off"


def test_service_credentials_are_secret(tmp_path):
    s = make_settings(tmp_path, yattee_username="serendip", yattee_password="hunter2-pw")
    assert s.service_user_configured
    assert "hunter2-pw" not in repr(s) and "hunter2-pw" not in str(s)
    assert "hunter2-pw" not in s.model_dump_json()
    assert s.service_authorization() == basic("serendip", "hunter2-pw")
    assert make_settings(tmp_path, yattee_username="serendip").service_user_configured is False
    assert make_settings(tmp_path, yattee_password="x").service_user_configured is False


def test_stage_b_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("SERENDIP_YATTEE_USERNAME", "serendip")
    monkeypatch.setenv("SERENDIP_YATTEE_PASSWORD", "pw")
    monkeypatch.setenv("SERENDIP_CARRY_MAX", "0.5")
    monkeypatch.setenv("SERENDIP_RELATED_SOURCE", "off")
    monkeypatch.setenv("SERENDIP_BREAKER_START_OPEN", "true")
    s = Settings(_env_file=None)
    assert s.service_user_configured and s.carry_max == 0.5 and s.related_source == "off"
    assert s.breaker_start_open is True

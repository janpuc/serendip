import pytest
from conftest import make_settings
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

import re

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_USER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _user_entries(value: str) -> list[tuple[str, str]]:
    entries = []
    for item in value.split(","):
        if item.strip():
            name, _, digest = item.partition("=")
            entries.append((name.strip(), digest.strip()))
    return entries


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SERENDIP_", env_file=".env", extra="ignore")

    version: str = "dev"
    log_level: str = "INFO"
    db_path: str = "/data/serendip.db"

    # Called in-cluster, never through the public hostname, so serendip's own
    # requests are not mirrored back to it.
    yattee_url: str = "http://yattee-server.media.svc.cluster.local:8085"
    api_port: int = Field(8080, gt=0, le=65535)
    # Mirrored traffic only; reachable through the gateway's mirror rules.
    ingest_port: int = Field(8081, gt=0, le=65535)

    # "serendip-tv=tv,serendip-phone=phone": a User-Agent containing the left side is the
    # device on the right. Anything else is 'unknown'.
    device_ua_map: str = ""

    # Comma-separated `name=<sha256>`: the lowercase hex sha256 of the full Authorization header
    # value, e.g. sha256("Basic amFucHVjOnNlY3JldA=="). Nothing is ever forwarded to check it.
    users: str = ""

    ingest_queue_max: int = Field(1000, gt=0)
    max_body_bytes: int = Field(65536, gt=0)
    # Total time to receive a mirrored body; a slow sender must not hold a connection.
    body_timeout_s: float = Field(10.0, gt=0)

    @field_validator("users")
    @classmethod
    def _check_users(cls, value: str) -> str:
        names, hashes = set(), set()
        for name, digest in _user_entries(value):
            if not _USER_NAME.match(name):
                raise ValueError("user names must match [A-Za-z0-9_.-]{1,64}")
            if not _SHA256_HEX.match(digest):
                raise ValueError("expected name=<lowercase hex sha256> entries")
            if name in names or digest in hashes:
                raise ValueError("duplicate user name or hash")
            names.add(name)
            hashes.add(digest)
        return value

    def user_names_by_hash(self) -> dict[str, str]:
        return {digest: name for name, digest in _user_entries(self.users)}

    def device_map(self) -> list[tuple[str, str]]:
        pairs = []
        for item in self.device_ua_map.split(","):
            marker, _, device = item.partition("=")
            if marker.strip() and device.strip():
                pairs.append((marker.strip().lower(), device.strip()))
        return pairs

    def device_for(self, user_agent: str) -> str:
        ua = user_agent.lower()
        for marker, device in self.device_map():
            if marker in ua:
                return device
        return "unknown"

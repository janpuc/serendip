import base64
import re
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
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

    # serendip's own non-admin Yattee Server user for background calls (DESIGN 3.1). Both are
    # needed; without them the background jobs are off and /popular still serves from the pool.
    yattee_username: str | None = None
    yattee_password: SecretStr | None = None
    # Whole-request limit for one call to Yattee Server; a cold extraction inside it can be slow.
    server_timeout_s: float = Field(90.0, gt=0)

    # "followup" asks the server for a video's related list after an open; "off" leaves the pool
    # to the subscriptions feed. "next" is reserved for a later stage and refused.
    related_source: Literal["followup", "off", "next"] = "followup"

    list_size: int = Field(30, ge=1, le=100)
    head_size: int = Field(5, ge=0, le=20)
    carry_max: float = Field(0.3, ge=0, le=0.5)
    repeat_window_s: float = Field(15.0, ge=0)
    shorts_max_seconds: int = Field(180, ge=0)
    timezone: str = "Europe/Warsaw"

    followup_delay_s: float = Field(60.0, ge=0)
    followup_deadline_s: float = Field(120.0, gt=0)
    followup_per_hour: int = Field(2, ge=0)
    followup_per_day: int = Field(20, ge=0)
    followup_min_spacing_s: float = Field(600.0, ge=0)

    # Opens the breaker on the very first start with a fresh database, so no job runs until
    # `serendip breaker reset`. Later starts ignore it.
    breaker_start_open: bool = False

    feed_interval_s: float = Field(21600.0, gt=0)
    feed_limit: int = Field(1000, ge=1, le=1000)

    @field_validator("related_source")
    @classmethod
    def _refuse_next(cls, value: str) -> str:
        if value == "next":
            raise ValueError(
                "SERENDIP_RELATED_SOURCE=next is reserved for a later stage and is not available;"
                " use 'followup' or 'off'"
            )
        return value

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("yattee_username")
    @classmethod
    def _check_username(cls, value: str | None) -> str | None:
        if value is not None and (not value or ":" in value):
            raise ValueError("the service user name must be non-empty and contain no ':'")
        return value

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

    @property
    def service_user_configured(self) -> bool:
        return bool(self.yattee_username and self.yattee_password)

    def service_authorization(self) -> str | None:
        """The Authorization header for serendip's own calls to Yattee Server.

        Built on demand and never stored, logged or put in a repr.
        """
        if not (self.yattee_username and self.yattee_password):
            return None
        raw = f"{self.yattee_username}:{self.yattee_password.get_secret_value()}"
        return "Basic " + base64.b64encode(raw.encode()).decode()

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

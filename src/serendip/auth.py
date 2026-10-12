"""Recognises a client's Authorization header from a local allowlist (DESIGN 3.1).

serendip never forwards a credential anywhere. Yattee Server counts failed authentications per
peer IP, 5 in 60 s, and checks that count before it validates the credentials, so any check
serendip made from the cluster could trip the limiter and then block valid checks as well.

SERENDIP_USERS lists `name=<sha256 of the full Authorization header value>`. The deployment
derives those hashes from the same 1Password item as the server's user, so a password change
updates both. A header whose hash is not listed is an unknown credential and its events are
dropped. The header and its hash are never logged: the hash is enough to test password guesses.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass

from . import metrics
from .config import Settings

USER = "user"
UNKNOWN_CREDENTIAL = "unknown_credential"
MALFORMED = "malformed"


@dataclass(frozen=True)
class Verdict:
    result: str  # USER, UNKNOWN_CREDENTIAL or MALFORMED
    name: str | None = None


def is_well_formed_basic(header: str | None) -> bool:
    if not header:
        return False
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() != "basic" or not credentials.strip():
        return False
    try:
        decoded = base64.b64decode(credentials.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, sep, _password = decoded.partition(":")
    return bool(sep and user)


class Authenticator:
    def __init__(self, settings: Settings):
        self._names = settings.user_names_by_hash()

    @property
    def user_count(self) -> int:
        return len(self._names)

    def check(self, header: str | None) -> Verdict:
        if not is_well_formed_basic(header):
            verdict = Verdict(MALFORMED)
        else:
            assert header is not None
            name = self._names.get(hashlib.sha256(header.encode()).hexdigest())
            verdict = Verdict(USER, name) if name else Verdict(UNKNOWN_CREDENTIAL)
        metrics.auth_checks.labels(verdict.result).inc()
        return verdict

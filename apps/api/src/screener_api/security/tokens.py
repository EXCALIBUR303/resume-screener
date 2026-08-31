"""JWT access tokens and opaque refresh tokens.

Access tokens carry identity only — ``sub`` and ``sid``. **Roles are never in
the token.** They are loaded from the database on every request, so a forged or
stale claim cannot grant permissions (rule C.2, privilege escalation).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid
from dataclasses import dataclass

import jwt

ALGORITHM = "HS256"  # Pinned. Decoding accepts exactly this — never a list, never 'none'.


class TokenError(Exception):
    """Raised for any invalid, expired, or malformed token."""


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID


def create_access_token(
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    secret: str,
    ttl_seconds: int,
    issuer: str,
    audience: str,
) -> str:
    now = dt.datetime.now(dt.UTC)
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        "iat": now,
        "nbf": now,
        "exp": now + dt.timedelta(seconds=ttl_seconds),
        "iss": issuer,
        "aud": audience,
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_access_token(token: str, *, secret: str, issuer: str, audience: str) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],  # exactly one; blocks alg confusion and 'none'
            issuer=issuer,
            audience=audience,
            options={"require": ["exp", "iat", "nbf", "sub", "sid", "iss", "aud"]},
        )
        return AccessClaims(user_id=uuid.UUID(payload["sub"]), session_id=uuid.UUID(payload["sid"]))
    except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
        raise TokenError(str(exc)) from exc


def new_refresh_token() -> tuple[str, str]:
    """Return (plaintext, sha256). Only the hash is ever persisted."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()

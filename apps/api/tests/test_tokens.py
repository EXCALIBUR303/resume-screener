"""Token handling: pinned algorithm, no roles in the token, reuse-safe hashing."""

from __future__ import annotations

import datetime as dt
import uuid

import jwt
import pytest

from screener_api.security.tokens import (
    ALGORITHM,
    TokenError,
    create_access_token,
    decode_access_token,
    hash_refresh_token,
    new_refresh_token,
)

SECRET = "test-secret-value-at-least-32-bytes-long"
ISS, AUD = "resume-screener", "resume-screener-api"


def make(**over: object) -> str:
    kwargs: dict[str, object] = {
        "user_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "secret": SECRET,
        "ttl_seconds": 900,
        "issuer": ISS,
        "audience": AUD,
    }
    kwargs.update(over)
    return create_access_token(**kwargs)  # type: ignore[arg-type]


def test_roundtrip() -> None:
    uid, sid = uuid.uuid4(), uuid.uuid4()
    claims = decode_access_token(
        make(user_id=uid, session_id=sid), secret=SECRET, issuer=ISS, audience=AUD
    )
    assert claims.user_id == uid
    assert claims.session_id == sid


def test_token_carries_no_roles() -> None:
    """Roles must come from the database. A role claim in the token would be a
    privilege-escalation primitive the moment the secret leaks."""
    payload = jwt.decode(make(), SECRET, algorithms=[ALGORITHM], audience=AUD, issuer=ISS)
    for forbidden in ("roles", "role", "permissions", "scope", "is_admin", "org_id"):
        assert forbidden not in payload


def test_alg_none_is_rejected() -> None:
    forged = jwt.encode({"sub": str(uuid.uuid4()), "sid": str(uuid.uuid4())}, "", algorithm="none")
    with pytest.raises(TokenError):
        decode_access_token(forged, secret=SECRET, issuer=ISS, audience=AUD)


def test_wrong_secret_is_rejected() -> None:
    with pytest.raises(TokenError):
        decode_access_token(make(), secret="a-different-secret", issuer=ISS, audience=AUD)


def test_wrong_audience_is_rejected() -> None:
    with pytest.raises(TokenError):
        decode_access_token(make(), secret=SECRET, issuer=ISS, audience="another-service")


def test_wrong_issuer_is_rejected() -> None:
    with pytest.raises(TokenError):
        decode_access_token(make(), secret=SECRET, issuer="somebody-else", audience=AUD)


def test_expired_token_is_rejected() -> None:
    with pytest.raises(TokenError):
        decode_access_token(make(ttl_seconds=-1), secret=SECRET, issuer=ISS, audience=AUD)


def test_garbage_is_rejected() -> None:
    for junk in ("", "not.a.token", "a.b.c", "Bearer x"):
        with pytest.raises(TokenError):
            decode_access_token(junk, secret=SECRET, issuer=ISS, audience=AUD)


def test_missing_required_claim_is_rejected() -> None:
    now = dt.datetime.now(dt.UTC)
    partial = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "iat": now,
            "exp": now + dt.timedelta(minutes=5),
            "iss": ISS,
            "aud": AUD,
        },  # no 'sid', no 'nbf'
        SECRET,
        algorithm=ALGORITHM,
    )
    with pytest.raises(TokenError):
        decode_access_token(partial, secret=SECRET, issuer=ISS, audience=AUD)


def test_refresh_tokens_are_unique_and_only_hashes_are_storable() -> None:
    raw_a, hash_a = new_refresh_token()
    raw_b, hash_b = new_refresh_token()
    assert raw_a != raw_b
    assert hash_a != hash_b
    assert hash_refresh_token(raw_a) == hash_a
    assert raw_a not in hash_a
    assert len(hash_a) == 64

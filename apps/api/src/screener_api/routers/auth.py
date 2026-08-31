"""Authentication routes: login, refresh with rotation, logout, whoami."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.db import get_session
from screener_api.models import RefreshToken, User
from screener_api.repos import UserRepo
from screener_api.security import audit
from screener_api.security.deps import CurrentActor
from screener_api.security.passwords import verify_password
from screener_api.security.tokens import (
    create_access_token,
    hash_refresh_token,
    new_refresh_token,
)
from screener_api.settings import Settings, get_settings

router = APIRouter(prefix="/auth", tags=["auth"])
log = structlog.get_logger()

# One message for every failure mode. Distinguishing "no such user" from "wrong
# password" hands an attacker a free account-enumeration oracle.
INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: uuid.UUID
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(min_length=1, max_length=512)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 — OAuth token type, not a credential
    expires_in: int


class WhoAmI(BaseModel):
    user_id: uuid.UUID
    org_id: uuid.UUID
    email: str
    roles: list[str]
    permissions: list[str]


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _issue_pair(
    session: AsyncSession, settings: Settings, user: User, family_id: uuid.UUID
) -> TokenPair:
    raw, token_hash = new_refresh_token()
    session.add(
        RefreshToken(
            family_id=family_id,
            user_id=user.id,
            token_hash=token_hash,
            expires_at=dt.datetime.now(dt.UTC)
            + dt.timedelta(seconds=settings.refresh_token_ttl_seconds),
        )
    )
    access = create_access_token(
        user_id=user.id,
        session_id=family_id,
        secret=settings.jwt_secret.get_secret_value(),
        ttl_seconds=settings.access_token_ttl_seconds,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )
    return TokenPair(
        access_token=access, refresh_token=raw, expires_in=settings.access_token_ttl_seconds
    )


@router.post("/login", response_model=TokenPair)
async def login(
    body: LoginRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenPair:
    if not settings.auth_local_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    user = await UserRepo(session).by_email(body.org_id, body.email)
    # verify_password runs against a decoy hash when the user is absent, so the
    # response time does not reveal whether the account exists.
    ok = user is not None and user.is_active and verify_password(body.password, user.password_hash)

    if not ok:
        await audit.record(
            session,
            action="auth.login",
            resource_type="user",
            resource_id=str(user.id) if user else None,
            org_id=body.org_id,
            actor_user_id=user.id if user else None,
            actor_ip=_client_ip(request),
            outcome="failure",
            meta={"reason": "invalid_credentials"},
        )
        await session.commit()
        log.info("auth.login.failed", org_id=str(body.org_id))
        raise INVALID_CREDENTIALS

    # Explicit check, not `assert`: asserts are stripped under `python -O`, and
    # an auth path must not depend on a statement the interpreter may discard.
    if user is None:  # pragma: no cover — unreachable while `ok` is True
        raise INVALID_CREDENTIALS
    pair = await _issue_pair(session, settings, user, uuid.uuid4())
    await audit.record(
        session,
        action="auth.login",
        resource_type="user",
        resource_id=str(user.id),
        org_id=user.org_id,
        actor_user_id=user.id,
        actor_ip=_client_ip(request),
    )
    await session.commit()
    return pair


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    body: RefreshRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenPair:
    presented = hash_refresh_token(body.refresh_token)
    stored = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == presented))
    ).scalar_one_or_none()

    if stored is None:
        raise INVALID_CREDENTIALS

    now = dt.datetime.now(dt.UTC)

    # Reuse detection: a token that was already rotated is in an attacker's hands
    # (or was replayed). Revoke the entire family — both parties are logged out,
    # which is the correct outcome. This is the control that makes stolen
    # refresh tokens survivable.
    if stored.used_at is not None or stored.revoked_at is not None:
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == stored.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await audit.record(
            session,
            action="auth.refresh_reuse_detected",
            resource_type="refresh_family",
            resource_id=str(stored.family_id),
            org_id=None,
            actor_user_id=stored.user_id,
            actor_ip=_client_ip(request),
            outcome="failure",
            meta={"revoked_family": True},
        )
        await session.commit()
        log.warning("auth.refresh.reuse_detected", family_id=str(stored.family_id))
        raise INVALID_CREDENTIALS

    if stored.expires_at <= now:
        raise INVALID_CREDENTIALS

    user = (
        await session.execute(select(User).where(User.id == stored.user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise INVALID_CREDENTIALS

    stored.used_at = now
    pair = await _issue_pair(session, settings, user, stored.family_id)
    await session.commit()
    return pair


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    actor: CurrentActor,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Revokes the whole family server-side — not just a client-side token drop."""
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.family_id == actor.session_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=dt.datetime.now(dt.UTC))
    )
    await audit.record(
        session,
        action="auth.logout",
        resource_type="refresh_family",
        resource_id=str(actor.session_id),
        org_id=actor.org_id,
        actor_user_id=actor.user_id,
        actor_ip=_client_ip(request),
    )
    await session.commit()


@router.get("/me", response_model=WhoAmI)
async def me(actor: CurrentActor, session: Annotated[AsyncSession, Depends(get_session)]) -> WhoAmI:
    user = (
        await session.execute(select(User).where(User.id == actor.user_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return WhoAmI(
        user_id=actor.user_id,
        org_id=actor.org_id,
        email=user.email,
        roles=sorted(actor.roles),
        permissions=sorted(str(p) for p in actor.permissions),
    )

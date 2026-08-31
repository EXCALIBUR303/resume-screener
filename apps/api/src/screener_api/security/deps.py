"""Authentication and authorisation dependencies."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.params import Depends as DependsMarker
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from screener_api.db import get_session
from screener_api.models import User
from screener_api.security.roles import Permission, permissions_for
from screener_api.security.tokens import TokenError, decode_access_token
from screener_api.settings import Settings, get_settings

bearer = HTTPBearer(auto_error=False)

UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)
# Deliberately identical to a not-found response elsewhere: a 403 that
# distinguishes "exists but forbidden" from "does not exist" is an enumeration
# oracle. See test_authz_matrix.
FORBIDDEN = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted")


@dataclass(frozen=True)
class Actor:
    """The authenticated caller. Carries the tenant scope every query needs."""

    user_id: uuid.UUID
    org_id: uuid.UUID
    session_id: uuid.UUID
    roles: frozenset[str]
    permissions: frozenset[Permission]

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions


async def get_actor(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Actor:
    if credentials is None or not credentials.credentials:
        raise UNAUTHENTICATED
    try:
        claims = decode_access_token(
            credentials.credentials,
            secret=settings.jwt_secret.get_secret_value(),
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
        )
    except TokenError:
        raise UNAUTHENTICATED from None

    # Roles come from the database, never from the token.
    user = (
        await session.execute(
            select(User).where(User.id == claims.user_id).options(selectinload(User.roles))
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise UNAUTHENTICATED

    request.state.actor_id = str(user.id)
    roles = user.role_names
    return Actor(
        user_id=user.id,
        org_id=user.org_id,
        session_id=claims.session_id,
        roles=roles,
        permissions=permissions_for(roles),
    )


CurrentActor = Annotated[Actor, Depends(get_actor)]


def requires(permission: Permission) -> DependsMarker:
    """Declare the permission a route needs.

    Every non-public route must use this. `test_authz_matrix` enumerates the
    router and fails the build on any route that declares nothing (AC-6).
    """

    async def _check(actor: CurrentActor) -> Actor:
        if not actor.can(permission):
            raise FORBIDDEN
        return actor

    _check.__required_permission__ = permission  # type: ignore[attr-defined]
    marker: DependsMarker = Depends(_check)
    return marker

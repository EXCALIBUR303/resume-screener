"""Admin routes. Separate router with its own permission gates (ASVS V4.3)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.db import get_session
from screener_api.models import AuditEvent
from screener_api.repos import UserRepo
from screener_api.security.audit import ChainBrokenError, verify_chain
from screener_api.security.deps import Actor, requires
from screener_api.security.roles import Permission

router = APIRouter(prefix="/admin", tags=["admin"])


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    display_name: str
    is_active: bool


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    seq: int
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    actor_user_id: uuid.UUID | None
    hash: str


class ChainStatus(BaseModel):
    valid: bool
    events_verified: int
    broken_at_seq: int | None = None
    reason: str | None = None


@router.get("/users", response_model=list[UserOut])
async def list_users(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[Actor, requires(Permission.USER_READ)],
) -> list[UserOut]:
    users = await UserRepo(session).list(actor)
    return [UserOut.model_validate(u) for u in users]


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(
    user_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[Actor, requires(Permission.USER_READ)],
) -> UserOut:
    user = await UserRepo(session).get(actor, user_id)
    if user is None:
        # 404 for another org's user, identical to a genuinely absent one.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return UserOut.model_validate(user)


@router.get("/audit", response_model=list[AuditEventOut])
async def list_audit(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[Actor, requires(Permission.AUDIT_READ)],
    limit: int = 50,
) -> list[AuditEventOut]:
    """The auditor role reaches this and nothing else — it sees that things
    happened, never what they contained."""
    events = (
        (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.org_id == actor.org_id)
                .order_by(AuditEvent.seq.desc())
                .limit(min(limit, 200))
            )
        )
        .scalars()
        .all()
    )
    return [AuditEventOut.model_validate(e) for e in events]


@router.get("/audit/verify", response_model=ChainStatus)
async def verify_audit_chain(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[Actor, requires(Permission.AUDIT_READ)],
) -> ChainStatus:
    try:
        count = await verify_chain(session)
    except ChainBrokenError as exc:
        return ChainStatus(valid=False, events_verified=0, broken_at_seq=exc.seq, reason=exc.reason)
    return ChainStatus(valid=True, events_verified=count)

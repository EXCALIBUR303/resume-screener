"""Admin routes. Separate router with its own permission gates (ASVS V4.3)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.db import get_session
from screener_api.models import AuditEvent
from screener_api.repos import UserRepo
from screener_api.security import audit
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


class DeadJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    job_type: str
    attempts: int
    error_class: str | None
    last_error: str | None


@router.get("/dlq", response_model=list[DeadJobOut])
async def list_dead_letters(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[Actor, requires(Permission.DLQ_MANAGE)],
    limit: int = 50,
) -> list[DeadJobOut]:
    """Jobs that were abandoned. Alert on this being non-empty."""
    from screener_api.queue import dead_letters

    return [DeadJobOut.model_validate(j) for j in await dead_letters(session, limit=limit)]


@router.post("/dlq/{job_id}/replay", status_code=status.HTTP_202_ACCEPTED)
async def replay_dead_letter(
    job_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[Actor, requires(Permission.DLQ_MANAGE)],
) -> dict[str, str]:
    """Return one dead job to the queue with a fresh attempt budget.

    Audited: replaying work that previously failed is exactly the kind of
    operator action that needs to be attributable afterwards.
    """
    from screener_api.queue import replay

    if not await replay(session, job_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No dead job with that id")

    await audit.record(
        session,
        action="dlq.replayed",
        resource_type="job",
        resource_id=str(job_id),
        org_id=actor.org_id,
        actor_user_id=actor.user_id,
        actor_ip=request.client.host if request.client else None,
    )
    await session.commit()
    return {"status": "requeued", "job_id": str(job_id)}


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

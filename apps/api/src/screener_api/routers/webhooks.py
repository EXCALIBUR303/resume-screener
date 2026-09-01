"""Webhook endpoint management.

The security-relevant decisions are all in the create path:

* The URL is validated as an SSRF destination **before it is stored**. Storing
  first and checking at delivery time would leave a queue of requests aimed at
  the instance metadata service, and a stored bad URL is a bad URL that
  survives a restart.
* The signing secret is generated here, not supplied by the caller, so its
  entropy is ours rather than whatever a client felt like sending.
* It is returned exactly once, in the creation response, and never again. The
  stored form is an AES-256-GCM envelope under a webhook-purpose key; there is
  no endpoint that reads it back.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.db import get_session
from screener_api.models import OutboxEvent, WebhookEndpoint
from screener_api.outbox.events import EventType
from screener_api.outbox.ssrf import DestinationRefusedError, validate
from screener_api.security import audit
from screener_api.security.crypto import WEBHOOK_KEY_PURPOSE, derive_kek, encrypt
from screener_api.security.deps import Actor, requires
from screener_api.security.roles import Permission
from screener_api.settings import Settings, get_settings

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

SECRET_BYTES = 32


class EndpointCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=2048)
    description: str = Field(default="", max_length=200)
    # Empty means every event type. Anything else is an explicit opt-in.
    event_types: list[str] = Field(default_factory=list, max_length=20)


class EndpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    url: str
    description: str
    event_types: list[str]
    is_active: bool
    disabled_reason: str | None
    consecutive_failures: int


class EndpointCreated(EndpointOut):
    # Shown once. There is deliberately no route that returns it again.
    signing_secret: str


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    event_type: str
    resource_id: str
    status: str
    attempts: int
    last_status_code: int | None
    last_error: str | None


@router.post("", response_model=EndpointCreated, status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    request: Request,
    body: EndpointCreate,
    actor: Annotated[Actor, requires(Permission.ORG_ADMIN_SETTINGS)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EndpointCreated:
    unknown = sorted(set(body.event_types) - {str(e) for e in EventType})
    if unknown:
        # Subscribing to an event nothing emits is a silent no-op, and the
        # tenant would sit waiting for a webhook that cannot arrive.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown event types: {', '.join(unknown)}",
        )

    try:
        validate(body.url, allow_private=settings.webhook_allow_private_destinations)
    except DestinationRefusedError as exc:
        await audit.record(
            session,
            action="webhook.rejected",
            resource_type="webhook_endpoint",
            org_id=actor.org_id,
            actor_user_id=actor.user_id,
            outcome="denied",
            meta={"reason": str(exc)[:200]},
        )
        await session.commit()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    secret = secrets.token_bytes(SECRET_BYTES)
    envelope = encrypt(
        secret,
        kek=derive_kek(
            settings.app_kek.get_secret_value(),
            settings.app_kek_version,
            purpose=WEBHOOK_KEY_PURPOSE,
        ),
        kek_version=settings.app_kek_version,
        aad=str(actor.org_id).encode(),
    )

    endpoint = WebhookEndpoint(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        url=body.url,
        description=body.description,
        secret_ciphertext=envelope.to_bytes(),
        event_types=body.event_types,
        created_by=actor.user_id,
    )
    session.add(endpoint)
    await audit.record(
        session,
        action="webhook.created",
        resource_type="webhook_endpoint",
        resource_id=str(endpoint.id),
        org_id=actor.org_id,
        actor_user_id=actor.user_id,
        actor_ip=request.client.host if request.client else None,
        # The URL is not in the audit meta. It is tenant-supplied text and the
        # audit log is read by people who are not that tenant.
        meta={"event_types": body.event_types},
    )
    await session.commit()

    return EndpointCreated(
        id=endpoint.id,
        url=endpoint.url,
        description=endpoint.description,
        event_types=list(endpoint.event_types),
        is_active=endpoint.is_active,
        disabled_reason=endpoint.disabled_reason,
        consecutive_failures=endpoint.consecutive_failures,
        signing_secret=secret.hex(),
    )


@router.get("", response_model=list[EndpointOut])
async def list_endpoints(
    actor: Annotated[Actor, requires(Permission.ORG_ADMIN_SETTINGS)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[WebhookEndpoint]:
    rows = await session.execute(
        select(WebhookEndpoint).where(WebhookEndpoint.org_id == actor.org_id)
    )
    return list(rows.scalars())


@router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    request: Request,
    endpoint_id: uuid.UUID,
    actor: Annotated[Actor, requires(Permission.ORG_ADMIN_SETTINGS)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    endpoint = (
        await session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.id == endpoint_id,
                WebhookEndpoint.org_id == actor.org_id,
            )
        )
    ).scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no such endpoint")

    await session.delete(endpoint)
    await audit.record(
        session,
        action="webhook.deleted",
        resource_type="webhook_endpoint",
        resource_id=str(endpoint_id),
        org_id=actor.org_id,
        actor_user_id=actor.user_id,
        actor_ip=request.client.host if request.client else None,
    )
    await session.commit()


@router.get("/deliveries", response_model=list[DeliveryOut])
async def list_deliveries(
    actor: Annotated[Actor, requires(Permission.ORG_ADMIN_SETTINGS)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 50,
) -> list[OutboxEvent]:
    """Recent events and what happened to them.

    A tenant whose receiver has been returning 500 for a day should be able to
    see that without asking us.
    """
    rows = await session.execute(
        select(OutboxEvent)
        .where(OutboxEvent.org_id == actor.org_id)
        .order_by(OutboxEvent.created_at.desc())
        .limit(min(limit, 200))
    )
    return list(rows.scalars())

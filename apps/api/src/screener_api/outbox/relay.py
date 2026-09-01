"""Outbox relay: reads events, delivers them, records what happened.

Claiming uses `FOR UPDATE SKIP LOCKED`, the same mechanism as the job queue
(ADR-0001), so several relays can run without coordinating and none of them
blocks on a row another one holds.

Delivery is **at-least-once**. A relay can post successfully and die before
committing the row, and the event is then delivered again. That is a property
of the design, not a bug in it: the alternative is at-most-once, which drops
events. Receivers deduplicate on `event_key`, which is stable across
redeliveries and is in the payload and the header.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.models import OutboxEvent, WebhookEndpoint
from screener_api.outbox.signing import sign
from screener_api.outbox.ssrf import Destination, DestinationRefusedError, validate
from screener_api.security.crypto import DecryptionError, Envelope, decrypt

log = structlog.get_logger()

DELIVERY_TIMEOUT_SECONDS = 10.0
# A receiver cannot make us read an unbounded body. The response is only read
# to put a fragment in `last_error`; nothing downstream parses it.
MAX_RESPONSE_BYTES = 4096
# Disable an endpoint after this many consecutive failures. A tenant who
# deleted their receiver should not cost a request every minute forever.
DISABLE_AFTER_FAILURES = 20


@dataclass(frozen=True)
class DeliveryOutcome:
    delivered: bool
    status_code: int | None
    error: str | None


def backoff_seconds(attempt: int) -> float:
    """Exponential with full jitter, capped at an hour.

    Jittered because a receiver that fell over will have every pending event
    for every tenant retrying against it. Synchronised retries are how a brief
    outage becomes a sustained one.
    """
    ceiling = min(3600.0, 2.0**attempt)
    # Retry jitter, not a security decision. noqa silences ruff, nosec silences
    # bandit; same pair as queue.py, for the same reason.
    return random.uniform(0, ceiling)  # noqa: S311  # nosec B311


async def claim(session: AsyncSession, *, worker: str, limit: int = 10) -> list[OutboxEvent]:
    stmt = text(
        """
        WITH picked AS (
            SELECT id FROM outbox_events
             WHERE status = 'pending'
               AND next_attempt_at <= now()
             ORDER BY next_attempt_at
             FOR UPDATE SKIP LOCKED
             LIMIT :limit
        )
        UPDATE outbox_events e
           SET status = 'delivering',
               locked_at = now(),
               locked_by = :worker,
               attempts = e.attempts + 1
          FROM picked
         WHERE e.id = picked.id
        RETURNING e.id
        """
    )
    ids = list((await session.execute(stmt, {"limit": limit, "worker": worker})).scalars())
    if not ids:
        return []
    return list(
        (await session.execute(select(OutboxEvent).where(OutboxEvent.id.in_(ids)))).scalars()
    )


async def endpoints_for(
    session: AsyncSession, *, org_id: uuid.UUID, event_type: str
) -> list[WebhookEndpoint]:
    rows = (
        await session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.org_id == org_id,
                WebhookEndpoint.is_active.is_(True),
            )
        )
    ).scalars()
    # An empty subscription list means "everything"; anything else is an
    # explicit opt-in per type.
    return [e for e in rows if not e.event_types or event_type in e.event_types]


def body_for(event: OutboxEvent) -> bytes:
    """Canonical JSON, so the bytes that were signed are the bytes that were sent.

    `sort_keys` and a fixed separator matter: re-serialising the same dict can
    otherwise produce different bytes, and the receiver would compute a
    different MAC over a payload we consider identical.
    """
    document: dict[str, Any] = {
        "id": str(event.id),
        "event_key": event.event_key,
        "type": event.event_type,
        "resource": {"type": event.resource_type, "id": event.resource_id},
        "occurred_at": event.created_at.isoformat(),
        "attempt": event.attempts,
        "data": event.payload,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


async def deliver(
    client: httpx.AsyncClient,
    *,
    destination: Destination,
    secret: bytes,
    event: OutboxEvent,
    now: int,
) -> DeliveryOutcome:
    body = body_for(event)
    headers = {
        "content-type": "application/json",
        "user-agent": "screener-webhooks/1",
        "x-screener-event-id": str(event.id),
        "x-screener-event-key": event.event_key,
        "x-screener-event-type": event.event_type,
        "x-screener-timestamp": str(now),
        "x-screener-signature": sign(secret, timestamp=now, body=body),
    }
    try:
        response = await client.post(
            destination.url,
            content=body,
            headers=headers,
            timeout=DELIVERY_TIMEOUT_SECONDS,
            # Redirects are disabled deliberately. Following one would let a
            # validated public URL bounce the request to 169.254.169.254 —
            # every address check above, undone by a 302.
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        return DeliveryOutcome(False, None, f"{type(exc).__name__}: {exc}"[:400])

    if 200 <= response.status_code < 300:
        return DeliveryOutcome(True, response.status_code, None)
    snippet = response.text[:200] if len(response.content) <= MAX_RESPONSE_BYTES else ""
    return DeliveryOutcome(False, response.status_code, f"HTTP {response.status_code} {snippet}")


def _settle(event: OutboxEvent, outcome: DeliveryOutcome) -> None:
    now = dt.datetime.now(dt.UTC)
    event.locked_at = None
    event.locked_by = None
    event.last_status_code = outcome.status_code
    event.last_error = outcome.error

    if outcome.delivered:
        event.status = "delivered"
        event.delivered_at = now
        return

    if event.attempts >= event.max_attempts:
        # Dead, not deleted. An event nobody could deliver is evidence, and the
        # DLQ is how an operator finds out a tenant's receiver has been broken
        # for a day.
        event.status = "dead"
        return

    event.status = "pending"
    event.next_attempt_at = now + dt.timedelta(seconds=backoff_seconds(event.attempts))


async def process(
    session: AsyncSession,
    client: httpx.AsyncClient,
    event: OutboxEvent,
    *,
    kek: bytes,
    now: int,
    allow_private: bool = False,
) -> DeliveryOutcome:
    """Deliver one event to every subscribed endpoint.

    An event succeeds when every subscribed endpoint accepted it. Partial
    success retries the whole event, so a receiver that already accepted it
    sees it again — which is exactly why `event_key` is documented as the
    deduplication key rather than left implicit.
    """
    targets = await endpoints_for(session, org_id=event.org_id, event_type=event.event_type)
    if not targets:
        outcome = DeliveryOutcome(True, None, None)  # nobody is listening; not a failure
        _settle(event, outcome)
        return outcome

    failures: list[str] = []
    last_code: int | None = None

    for endpoint in targets:
        try:
            # Re-validated on every attempt, not trusted from creation time. An
            # endpoint stored months ago points at whatever its DNS says today.
            destination = validate(endpoint.url, allow_private=allow_private)
        except DestinationRefusedError as exc:
            failures.append(f"{endpoint.id}: refused: {exc}")
            _disable(endpoint, f"destination refused: {exc}")
            continue

        try:
            secret = decrypt(
                Envelope.from_bytes(endpoint.secret_ciphertext),
                kek=kek,
                aad=str(endpoint.org_id).encode(),
            )
        except DecryptionError as exc:
            failures.append(f"{endpoint.id}: secret undecryptable")
            log.error("webhook.secret_undecryptable", endpoint_id=str(endpoint.id), error=str(exc))
            continue

        outcome = await deliver(
            client, destination=destination, secret=secret, event=event, now=now
        )
        last_code = outcome.status_code or last_code
        if outcome.delivered:
            endpoint.consecutive_failures = 0
        else:
            failures.append(f"{endpoint.id}: {outcome.error}")
            endpoint.consecutive_failures += 1
            if endpoint.consecutive_failures >= DISABLE_AFTER_FAILURES:
                _disable(endpoint, f"{endpoint.consecutive_failures} consecutive failures")

    result = (
        DeliveryOutcome(True, last_code, None)
        if not failures
        else DeliveryOutcome(False, last_code, "; ".join(failures)[:400])
    )
    _settle(event, result)
    return result


def _disable(endpoint: WebhookEndpoint, reason: str) -> None:
    endpoint.is_active = False
    endpoint.disabled_reason = reason[:200]
    log.warning("webhook.endpoint_disabled", endpoint_id=str(endpoint.id), reason=reason)

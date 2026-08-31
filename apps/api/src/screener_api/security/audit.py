"""Tamper-evident audit log.

Each row commits ``hash = sha256(prev_hash || canonical_json(payload))`` in the
same transaction as the change it records. Rewriting or deleting any row breaks
every link after it, which ``verify_chain`` detects and names.

Two properties this buys, both of which matter for the erasure/auditability
tension described in the blueprint:

1. Deleting a candidate can remove all their content while the chain stays
   valid, because rows hold hashes and metadata, never raw personal data.
2. A database superuser can still edit rows — no application can prevent that —
   but they cannot do it *undetectably*.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.models import AuditEvent

# The chain's anchor. A row claiming this as prev_hash must be seq 1.
GENESIS = "0" * 64

# Fields that are hashed, in this order. Adding a field here changes every
# subsequent hash, so it is a migration, not an edit.
HASHED_FIELDS = (
    "id",
    "org_id",
    "actor_user_id",
    "actor_ip_hash",
    "action",
    "resource_type",
    "resource_id",
    "outcome",
    "meta",
)


def canonical_json(payload: dict[str, Any]) -> str:
    """Deterministic serialisation: sorted keys, no whitespace, no NaN.

    Two structurally identical payloads must produce byte-identical output on
    any machine, or the chain is not reproducible.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    )


def compute_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical_json(payload)).encode()).hexdigest()


def hash_ip(ip: str | None) -> str | None:
    """IP addresses are personal data. Store a hash so abuse patterns are still
    detectable without retaining the address itself."""
    return hashlib.sha256(ip.encode()).hexdigest() if ip else None


def _payload(event: AuditEvent) -> dict[str, Any]:
    return {f: getattr(event, f) for f in HASHED_FIELDS}


async def record(
    session: AsyncSession,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    org_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    actor_ip: str | None = None,
    outcome: str = "success",
    meta: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one event. Must run inside the caller's transaction."""
    prev = (
        await session.execute(
            select(AuditEvent.hash).order_by(AuditEvent.seq.desc()).limit(1).with_for_update()
        )
    ).scalar_one_or_none()

    event = AuditEvent(
        id=uuid.uuid4(),
        org_id=org_id,
        actor_user_id=actor_user_id,
        actor_ip_hash=hash_ip(actor_ip),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        meta=meta or {},
        prev_hash=prev or GENESIS,
    )
    event.hash = compute_hash(event.prev_hash, _payload(event))
    session.add(event)
    return event


class ChainBrokenError(Exception):
    def __init__(self, seq: int, reason: str) -> None:
        super().__init__(f"audit chain broken at seq={seq}: {reason}")
        self.seq, self.reason = seq, reason


async def verify_chain(session: AsyncSession) -> int:
    """Walk the whole chain. Returns the number of events verified.

    Raises ChainBrokenError naming the first row that does not reconcile.
    """
    events = (await session.execute(select(AuditEvent).order_by(AuditEvent.seq))).scalars().all()
    expected_prev = GENESIS
    for event in events:
        if event.prev_hash != expected_prev:
            raise ChainBrokenError(event.seq, "prev_hash does not match the preceding row's hash")
        recomputed = compute_hash(event.prev_hash, _payload(event))
        if recomputed != event.hash:
            raise ChainBrokenError(event.seq, "row contents do not match its stored hash")
        expected_prev = event.hash
    return len(events)

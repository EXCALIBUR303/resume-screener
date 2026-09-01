"""Domain events, and what may be inside one.

Every event is written by `record()` **using the caller's session**, which is
the entire point: the event and the change it describes commit or roll back
together. A function here must never open its own session or commit — doing so
would restore exactly the split-brain the outbox exists to prevent.

The payload rule is the audit log's rule, applied harder. An audit row stays
inside our database; a webhook payload leaves our network for a URL a tenant
chose. Identifiers and non-identifying metadata only. `assert_no_pii` enforces
it at the boundary rather than trusting each call site to remember.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

import structlog
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.models import OutboxEvent

log = structlog.get_logger()


class EventType(StrEnum):
    """Only events that are actually emitted.

    A member nobody writes is the same defect as a recognizer that cannot
    fire: it advertises coverage that does not exist, and a tenant subscribing
    to it would wait forever for something no code path produces.
    """

    RESUME_PARSED = "resume.parsed"
    RESUME_SCORED = "resume.scored"


# Keys a payload may carry. An allowlist rather than a denylist: a denylist has
# to anticipate every field name that might one day hold a person's name, and
# it only has to be wrong once.
ALLOWED_KEYS = frozenset(
    {
        "resume_id",
        "candidate_id",
        "job_id",
        "match_id",
        "interview_id",
        "org_id",
        "score",
        "degraded",
        "partially_supported",
        "injection_suspected",
        "keyword_stuffing",
        "parse_status",
        "error_class",
        "model_id",
        "prompt_version",
        "occurred_at",
        "pipeline_version",
        "matched_skill_count",
        "missing_skill_count",
        "hard_gate_failures",
    }
)


class PayloadRejectedError(Exception):
    """A payload carried something that must not leave the network."""


def assert_no_pii(payload: dict[str, Any]) -> None:
    """Refuse anything outside the allowlist, and refuse long free text.

    The length check is a second net. A key could be added to the allowlist in
    good faith and then be used to carry a paragraph of resume prose; 200
    characters is far more than any identifier here needs and far less than a
    sentence worth leaking.
    """
    extra = sorted(set(payload) - ALLOWED_KEYS)
    if extra:
        raise PayloadRejectedError(f"keys not on the webhook allowlist: {', '.join(extra)}")
    for key, value in payload.items():
        if isinstance(value, str) and len(value) > 200:
            raise PayloadRejectedError(f"{key!r} is too long to be an identifier")


async def record(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    event_type: EventType,
    resource_type: str,
    resource_id: str,
    payload: dict[str, Any],
    event_key: str,
) -> uuid.UUID | None:
    """Write one event into the caller's transaction.

    Returns None when this event has already been recorded. The uniqueness is a
    database constraint rather than a check-then-insert, for the same reason the
    job queue's is: two workers racing on the same resource must produce one
    event, not two, and application logic cannot promise that.
    """
    assert_no_pii(payload)

    stmt = (
        insert(OutboxEvent)
        .values(
            id=uuid.uuid4(),
            org_id=org_id,
            event_type=str(event_type),
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload,
            event_key=event_key,
        )
        .on_conflict_do_nothing(index_elements=["event_key"])
        .returning(OutboxEvent.id)
    )
    event_id = (await session.execute(stmt)).scalar_one_or_none()
    if event_id is not None:
        log.info("outbox.recorded", event_type=str(event_type), resource_id=resource_id)
    return event_id

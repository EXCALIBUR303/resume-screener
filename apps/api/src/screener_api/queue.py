"""Postgres-backed job queue.

Delivery is at-least-once (a worker can die holding a lease), and results are
effectively-once because every job carries an idempotency key backed by a UNIQUE
constraint. That pairing is the whole design: see ADR-0001.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.models import JobQueue

log = structlog.get_logger()


class JobType(StrEnum):
    PARSE = "parse"
    EMBED = "embed"
    SCORE = "score"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    DEAD = "dead"


class ErrorClass(StrEnum):
    """Retryable vs terminal is decided explicitly, never inferred.

    A terminal failure skips retries entirely — retrying a schema-invalid
    document four more times just burns time and hides the real problem.
    """

    RETRYABLE = "retryable"
    TERMINAL = "terminal"


class TerminalError(Exception):
    """Raised by a handler when retrying cannot possibly help."""


@dataclass(frozen=True)
class Lease:
    job: JobQueue
    worker: str


def idempotency_key(
    job_type: str,
    *,
    input_digest: str,
    pipeline_version: str,
    prompt_version: str = "-",
    model_id: str = "-",
) -> str:
    """Identical work produces an identical key, so the UNIQUE constraint makes
    a duplicate enqueue a no-op rather than a second run."""
    raw = "\x1f".join([job_type, input_digest, pipeline_version, prompt_version, model_id])
    return hashlib.sha256(raw.encode()).hexdigest()


async def enqueue(
    session: AsyncSession,
    *,
    job_type: JobType,
    payload: dict[str, Any],
    key: str,
    org_id: uuid.UUID | None = None,
    priority: int = 0,
    max_attempts: int = 5,
    run_after: dt.datetime | None = None,
) -> uuid.UUID | None:
    """Insert a job. Returns None when an identical job already exists."""
    stmt = (
        insert(JobQueue)
        .values(
            id=uuid.uuid4(),
            org_id=org_id,
            job_type=str(job_type),
            payload=payload,
            idempotency_key=key,
            priority=priority,
            max_attempts=max_attempts,
            run_after=run_after or dt.datetime.now(dt.UTC),
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(JobQueue.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def claim(session: AsyncSession, *, worker: str, job_types: list[JobType]) -> JobQueue | None:
    """Atomically take one job.

    SKIP LOCKED lets N workers poll the same table without blocking each other:
    a row already locked by another transaction is passed over rather than
    waited on.
    """
    stmt = text(
        """
        WITH picked AS (
            SELECT id FROM job_queue
             WHERE status = 'pending'
               AND run_after <= now()
               AND job_type = ANY(:types)
             ORDER BY priority DESC, run_after
             FOR UPDATE SKIP LOCKED
             LIMIT 1
        )
        UPDATE job_queue q
           SET status = 'running',
               locked_at = now(),
               locked_by = :worker,
               attempts = q.attempts + 1
          FROM picked
         WHERE q.id = picked.id
        RETURNING q.id
        """
    )
    job_id = (
        await session.execute(stmt, {"types": [str(t) for t in job_types], "worker": worker})
    ).scalar_one_or_none()
    if job_id is None:
        return None
    return (await session.execute(select(JobQueue).where(JobQueue.id == job_id))).scalar_one()


async def complete(session: AsyncSession, job: JobQueue) -> None:
    job.status = str(JobStatus.DONE)
    job.finished_at = dt.datetime.now(dt.UTC)
    job.locked_at = None
    job.locked_by = None


async def fail(
    session: AsyncSession, job: JobQueue, *, error: BaseException, error_class: ErrorClass
) -> None:
    """Reschedule with backoff, or dead-letter.

    Terminal errors go straight to the DLQ. So do retryable ones that have
    exhausted their attempts — a job must never sit pending forever.
    """
    job.last_error = f"{type(error).__name__}: {error}"[:500]
    job.error_class = str(error_class)
    job.locked_at = None
    job.locked_by = None

    exhausted = job.attempts >= job.max_attempts
    if error_class is ErrorClass.TERMINAL or exhausted:
        job.status = str(JobStatus.DEAD)
        job.finished_at = dt.datetime.now(dt.UTC)
        log.warning(
            "job.dead",
            job_id=str(job.id),
            job_type=job.job_type,
            attempts=job.attempts,
            error_class=str(error_class),
        )
        return

    # Exponential backoff with full jitter: without jitter, a batch of jobs that
    # failed together retries together, and hammers whatever just broke.
    base = min(2**job.attempts, 300)
    # Retry jitter, not a security decision. noqa silences ruff, nosec silences
    # bandit; both flag this same line and both must be on it.
    delay = random.uniform(0, base)  # noqa: S311  # nosec B311
    job.status = str(JobStatus.PENDING)
    job.run_after = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delay)


async def reclaim_expired_leases(session: AsyncSession, *, timeout_seconds: int) -> int:
    """Return jobs abandoned by dead workers to the pending pool.

    This is what makes delivery at-least-once rather than at-most-once: a worker
    killed mid-job does not strand its work.
    """
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=timeout_seconds)
    result = await session.execute(
        update(JobQueue)
        .where(JobQueue.status == str(JobStatus.RUNNING), JobQueue.locked_at < cutoff)
        .values(
            status=str(JobStatus.PENDING),
            locked_at=None,
            locked_by=None,
            last_error="lease expired; worker presumed dead",
            error_class=str(ErrorClass.RETRYABLE),
        )
        .returning(JobQueue.id)
    )
    return len(result.scalars().all())


async def dead_letters(session: AsyncSession, *, limit: int = 50) -> list[JobQueue]:
    return list(
        (
            await session.execute(
                select(JobQueue)
                .where(JobQueue.status == str(JobStatus.DEAD))
                .order_by(JobQueue.finished_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def replay(session: AsyncSession, job_id: uuid.UUID) -> bool:
    """Return one dead job to the queue with a fresh attempt budget."""
    job = (
        await session.execute(select(JobQueue).where(JobQueue.id == job_id))
    ).scalar_one_or_none()
    if job is None or job.status != str(JobStatus.DEAD):
        return False
    job.status = str(JobStatus.PENDING)
    job.attempts = 0
    job.run_after = dt.datetime.now(dt.UTC)
    job.finished_at = None
    job.last_error = None
    job.error_class = None
    return True

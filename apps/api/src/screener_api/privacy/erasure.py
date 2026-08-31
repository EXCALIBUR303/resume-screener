"""Candidate erasure and retention.

The hard part is not deleting rows; it is deleting them *while keeping the audit
chain valid*. The chain must remain verifiable after the content it describes is
gone, which is only possible because audit rows hold hashes and metadata and
never raw personal data (see the M1 schema).

What a purge removes: the candidate, its resumes, extracted text (raw and
redacted), the encrypted PII map, queued jobs, the file rows, and the encrypted
blobs on disk.

What it leaves: audit events, rewritten to a tombstone that records *that* a
purge happened, by whom, and against which resource hash — never what the
resource contained.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.ingest.storage import BlobStore
from screener_api.models import (
    Candidate,
    JobQueue,
    PiiMap,
    Resume,
    ResumeText,
    StoredFile,
)
from screener_api.security import audit

log = structlog.get_logger()


@dataclass
class PurgeReport:
    candidate_id: uuid.UUID
    resumes: int = 0
    texts: int = 0
    pii_maps: int = 0
    files: int = 0
    blobs: int = 0
    jobs: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return self.resumes + self.texts + self.pii_maps + self.files + self.jobs


async def purge_candidate(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    *,
    org_id: uuid.UUID,
    store: BlobStore,
    actor_user_id: uuid.UUID | None = None,
    actor_ip: str | None = None,
    reason: str = "erasure_request",
) -> PurgeReport:
    """Remove every trace of a candidate. Idempotent: purging twice is safe."""
    report = PurgeReport(candidate_id=candidate_id)

    candidate = (
        await session.execute(
            select(Candidate).where(Candidate.id == candidate_id, Candidate.org_id == org_id)
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise LookupError(f"candidate {candidate_id} not found in this organisation")

    resumes = list(
        (await session.execute(select(Resume).where(Resume.candidate_id == candidate_id)))
        .scalars()
        .all()
    )
    resume_ids = [r.id for r in resumes]
    file_ids = {r.file_id for r in resumes}

    if resume_ids:
        report.texts = len(
            (
                await session.execute(
                    delete(ResumeText)
                    .where(ResumeText.resume_id.in_(resume_ids))
                    .returning(ResumeText.id)
                )
            )
            .scalars()
            .all()
        )
        report.pii_maps = len(
            (
                await session.execute(
                    delete(PiiMap).where(PiiMap.resume_id.in_(resume_ids)).returning(PiiMap.id)
                )
            )
            .scalars()
            .all()
        )
        # Queued work referencing a purged resume must go too, or a worker will
        # claim it and fail forever.
        report.jobs = len(
            (
                await session.execute(
                    delete(JobQueue)
                    .where(JobQueue.payload["resume_id"].astext.in_([str(r) for r in resume_ids]))
                    .returning(JobQueue.id)
                )
            )
            .scalars()
            .all()
        )
        report.resumes = len(
            (
                await session.execute(
                    delete(Resume).where(Resume.id.in_(resume_ids)).returning(Resume.id)
                )
            )
            .scalars()
            .all()
        )

    # Files are deleted only once nothing references them. The FK is RESTRICT
    # precisely so an ordering mistake fails loudly instead of orphaning blobs.
    digests: list[str] = []
    for file_id in file_ids:
        stored = (
            await session.execute(select(StoredFile).where(StoredFile.id == file_id))
        ).scalar_one_or_none()
        if stored is None:
            continue
        still_used = (
            await session.execute(select(Resume.id).where(Resume.file_id == file_id).limit(1))
        ).scalar_one_or_none()
        if still_used is not None:
            continue
        digests.append(stored.sha256)
        await session.execute(delete(StoredFile).where(StoredFile.id == file_id))
        report.files += 1

    await session.execute(delete(Candidate).where(Candidate.id == candidate_id))

    # The tombstone: enough to prove the purge happened and hold the chain
    # together, nothing that could reconstruct what was purged.
    await audit.record(
        session,
        action="candidate.purged",
        resource_type="candidate",
        resource_id=str(candidate_id),
        org_id=org_id,
        actor_user_id=actor_user_id,
        actor_ip=actor_ip,
        meta={
            "reason": reason,
            "resumes_removed": report.resumes,
            "texts_removed": report.texts,
            "pii_maps_removed": report.pii_maps,
            "files_removed": report.files,
            "jobs_removed": report.jobs,
            "purged_at": dt.datetime.now(dt.UTC).isoformat(),
        },
    )

    # Blobs last: if this fails the transaction can still roll back cleanly, and
    # a leftover blob is recoverable by the sweep. A deleted blob with live rows
    # would not be.
    for digest in digests:
        try:
            store.delete(digest)
            report.blobs += 1
        except Exception as exc:
            report.errors.append(f"blob {digest[:12]}: {type(exc).__name__}")
            log.error("purge.blob_failed", digest=digest[:12], error=type(exc).__name__)

    log.info(
        "candidate.purged",
        candidate_id=str(candidate_id),
        rows=report.total_rows,
        blobs=report.blobs,
        errors=len(report.errors),
    )
    return report


async def expired_candidates(
    session: AsyncSession, *, org_id: uuid.UUID, retention_days: int
) -> list[uuid.UUID]:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=retention_days)
    return list(
        (
            await session.execute(
                select(Candidate.id).where(
                    Candidate.org_id == org_id, Candidate.created_at < cutoff
                )
            )
        )
        .scalars()
        .all()
    )

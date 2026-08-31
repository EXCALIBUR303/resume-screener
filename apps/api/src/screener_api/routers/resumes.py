"""Resume upload and download.

The upload path is the project's main untrusted-input surface, so it is
deliberately linear and easy to audit:

    read (capped) -> validate -> quarantine -> scan -> promote -> record -> audit

Nothing is written to the clean store, and no database row is committed, until
validation has passed.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.db import get_session
from screener_api.ingest.storage import BlobStore
from screener_api.ingest.validation import Limits, UploadRejectedError, validate
from screener_api.models import Candidate, Resume, StoredFile
from screener_api.security import audit
from screener_api.security.crypto import derive_kek
from screener_api.security.deps import Actor, requires
from screener_api.security.roles import Permission
from screener_api.settings import Settings, get_settings

router = APIRouter(prefix="/resumes", tags=["resumes"])
log = structlog.get_logger()


class ResumeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    candidate_id: uuid.UUID
    parse_status: str
    created_at: object


class UploadAccepted(BaseModel):
    resume_id: uuid.UUID
    candidate_id: uuid.UUID
    file_id: uuid.UUID
    sha256: str
    byte_size: int
    page_count: int | None
    duplicate: bool


def get_store(settings: Annotated[Settings, Depends(get_settings)]) -> BlobStore:
    return BlobStore(
        settings.storage_local_path,
        kek=derive_kek(settings.app_kek.get_secret_value(), settings.app_kek_version),
        kek_version=settings.app_kek_version,
    )


def _limits(settings: Settings) -> Limits:
    return Limits(
        max_bytes=settings.upload_max_bytes,
        max_pages=settings.upload_max_pages,
        zip_max_ratio=settings.zip_max_ratio,
        zip_max_entries=settings.zip_max_entries,
        zip_max_uncompressed=settings.zip_max_uncompressed_bytes,
    )


@router.post("", response_model=UploadAccepted, status_code=status.HTTP_201_CREATED)
async def upload_resume(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[BlobStore, Depends(get_store)],
    actor: Annotated[Actor, requires(Permission.RESUME_WRITE)],
    file: Annotated[UploadFile, File()],
    external_ref: Annotated[str | None, Form()] = None,
) -> UploadAccepted:
    limits = _limits(settings)

    # Read with a hard ceiling. Reading the whole body first and checking the
    # length afterwards would let an attacker allocate memory at will.
    data = await file.read(limits.max_bytes + 1)
    if len(data) > limits.max_bytes:
        await _audit_rejection(session, actor, request, "too_large", len(data))
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"File exceeds the {limits.max_bytes // 1024 // 1024} MB limit.",
        )

    try:
        validated = validate(
            data,
            filename=file.filename,
            declared_mime=file.content_type,
            limits=limits,
        )
    except UploadRejectedError as rejection:
        await _audit_rejection(session, actor, request, rejection.reason, len(data))
        await session.commit()
        log.info("upload.rejected", reason=str(rejection.reason), byte_size=len(data))
        # The reason is safe to return: it describes the caller's own file and
        # helps a legitimate user fix it. It reveals nothing about other tenants.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"File rejected: {rejection.reason}"
        ) from rejection

    blob = store.put_quarantine(data, org_id=str(actor.org_id))

    existing = (
        await session.execute(
            select(StoredFile).where(
                StoredFile.org_id == actor.org_id, StoredFile.sha256 == blob.sha256
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Idempotent: the same bytes twice cost nothing and create nothing.
        store.discard(blob.sha256)
        resume = (
            await session.execute(
                select(Resume).where(Resume.org_id == actor.org_id, Resume.file_id == existing.id)
            )
        ).scalar_one_or_none()
        if resume is not None:
            return UploadAccepted(
                resume_id=resume.id,
                candidate_id=resume.candidate_id,
                file_id=existing.id,
                sha256=existing.sha256,
                byte_size=existing.byte_size,
                page_count=existing.page_count,
                duplicate=True,
            )

    # ClamAV lands here when the profile is enabled; until then the status is
    # recorded honestly as "skipped" rather than implying a scan happened.
    scan_status = "skipped"
    store.promote(blob.sha256)

    stored = StoredFile(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        sha256=blob.sha256,
        storage_key=blob.storage_key,
        byte_size=blob.byte_size,
        mime_declared=file.content_type,
        mime_sniffed=validated.sniffed_mime,
        mime_resolved=validated.mime,
        original_filename=(file.filename or "")[:255],
        page_count=validated.page_count,
        scan_status=scan_status,
        is_quarantined=False,
        uploaded_by=actor.user_id,
    )
    session.add(stored)
    await session.flush()

    candidate = Candidate(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        pseudonym=f"CANDIDATE_{blob.sha256[:8].upper()}",
        external_ref=external_ref[:120] if external_ref else None,
    )
    session.add(candidate)
    await session.flush()

    resume = Resume(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        candidate_id=candidate.id,
        file_id=stored.id,
        parse_status="pending",
    )
    session.add(resume)

    await audit.record(
        session,
        action="resume.uploaded",
        resource_type="resume",
        resource_id=str(resume.id),
        org_id=actor.org_id,
        actor_user_id=actor.user_id,
        actor_ip=request.client.host if request.client else None,
        meta={
            "sha256": blob.sha256,
            "byte_size": blob.byte_size,
            "mime": validated.mime,
            "pages": validated.page_count,
            "scan_status": scan_status,
        },
    )
    await session.commit()

    return UploadAccepted(
        resume_id=resume.id,
        candidate_id=candidate.id,
        file_id=stored.id,
        sha256=blob.sha256,
        byte_size=blob.byte_size,
        page_count=validated.page_count,
        duplicate=False,
    )


@router.get("/{resume_id}", response_model=ResumeOut)
async def get_resume(
    resume_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[Actor, requires(Permission.RESUME_READ)],
) -> ResumeOut:
    resume = (
        await session.execute(
            select(Resume).where(Resume.id == resume_id, Resume.org_id == actor.org_id)
        )
    ).scalar_one_or_none()
    if resume is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return ResumeOut.model_validate(resume)


@router.get("/{resume_id}/download")
async def download_resume(
    resume_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[BlobStore, Depends(get_store)],
    actor: Annotated[Actor, requires(Permission.RESUME_READ)],
) -> Response:
    """Serve the original bytes as a download only.

    Never inline: an uploaded document rendered in the app's own origin is a
    stored-XSS primitive. Attachment disposition, nosniff, and a sandboxing CSP
    together mean the browser cannot execute anything in this response.
    """
    row = (
        await session.execute(
            select(Resume, StoredFile)
            .join(StoredFile, Resume.file_id == StoredFile.id)
            .where(Resume.id == resume_id, Resume.org_id == actor.org_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    _, stored = row
    data = store.get(stored.sha256, org_id=str(actor.org_id))

    await audit.record(
        session,
        action="resume.downloaded",
        resource_type="resume",
        resource_id=str(resume_id),
        org_id=actor.org_id,
        actor_user_id=actor.user_id,
        actor_ip=request.client.host if request.client else None,
        meta={"sha256": stored.sha256},
    )
    await session.commit()

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            # A generated filename, never the uploader's string.
            "Content-Disposition": f'attachment; filename="resume-{stored.sha256[:12]}.bin"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "no-store, private",
        },
    )


async def _audit_rejection(
    session: AsyncSession, actor: Actor, request: Request, reason: object, size: int
) -> None:
    await audit.record(
        session,
        action="resume.upload_rejected",
        resource_type="upload",
        org_id=actor.org_id,
        actor_user_id=actor.user_id,
        actor_ip=request.client.host if request.client else None,
        outcome="failure",
        meta={"reason": str(reason), "byte_size": size},
    )

"""Candidate access and erasure."""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.db import get_session
from screener_api.ingest.storage import BlobStore
from screener_api.models import Candidate, PiiMap, ResumeText
from screener_api.privacy.erasure import purge_candidate
from screener_api.routers.resumes import get_store
from screener_api.security.crypto import DecryptionError, Envelope, decrypt, derive_kek
from screener_api.security.deps import Actor, requires
from screener_api.security.roles import Permission
from screener_api.settings import Settings, get_settings

router = APIRouter(prefix="/candidates", tags=["candidates"])
log = structlog.get_logger()


class PurgeResult(BaseModel):
    candidate_id: uuid.UUID
    resumes_removed: int
    texts_removed: int
    pii_maps_removed: int
    files_removed: int
    blobs_removed: int
    jobs_removed: int
    errors: list[str]


class CandidateView(BaseModel):
    """What a recruiter sees: the redacted text, re-hydrated for display.

    Re-hydration happens HERE, in the API process, for an authorised human. The
    model never sees this — it only ever received the tokenised form.
    """

    candidate_id: uuid.UUID
    pseudonym: str
    display_text: str
    entity_counts: dict[str, int]


@router.get("/{candidate_id}", response_model=CandidateView)
async def get_candidate(
    candidate_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    actor: Annotated[Actor, requires(Permission.RESUME_READ)],
) -> CandidateView:
    from screener_api.privacy.redact import rehydrate

    candidate = (
        await session.execute(
            select(Candidate).where(Candidate.id == candidate_id, Candidate.org_id == actor.org_id)
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    row = (
        await session.execute(
            select(ResumeText, PiiMap)
            .join(PiiMap, PiiMap.resume_id == ResumeText.resume_id, isouter=True)
            .join(
                Candidate,
                Candidate.id == candidate_id,
            )
            .where(ResumeText.org_id == actor.org_id)
            .limit(1)
        )
    ).first()
    if row is None:
        return CandidateView(
            candidate_id=candidate_id,
            pseudonym=candidate.pseudonym,
            display_text="",
            entity_counts={},
        )

    text_row, pii_map = row
    display = text_row.text_redacted or ""
    counts: dict[str, int] = {}

    if pii_map is not None:
        kek = derive_kek(settings.app_kek.get_secret_value(), settings.app_kek_version)
        try:
            import json

            token_map = json.loads(
                decrypt(
                    Envelope.from_bytes(pii_map.ciphertext),
                    kek=kek,
                    aad=str(actor.org_id).encode(),
                )
            )
            display = rehydrate(display, token_map)
            counts = {k: int(v) for k, v in pii_map.entity_counts.items()}
        except DecryptionError:
            # Show the tokenised form rather than failing: the recruiter still
            # gets the substance, and the failure is recorded for an operator.
            log.error("candidate.pii_map_undecryptable", candidate_id=str(candidate_id))

    return CandidateView(
        candidate_id=candidate_id,
        pseudonym=candidate.pseudonym,
        display_text=display,
        entity_counts=counts,
    )


@router.delete("/{candidate_id}", response_model=PurgeResult)
async def delete_candidate(
    candidate_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[BlobStore, Depends(get_store)],
    # Erasure is destructive and irreversible: owner only, never a recruiter.
    actor: Annotated[Actor, requires(Permission.ORG_ADMIN_SETTINGS)],
) -> PurgeResult:
    try:
        report = await purge_candidate(
            session,
            candidate_id,
            org_id=actor.org_id,
            store=store,
            actor_user_id=actor.user_id,
            actor_ip=request.client.host if request.client else None,
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from exc

    await session.commit()
    return PurgeResult(
        candidate_id=candidate_id,
        resumes_removed=report.resumes,
        texts_removed=report.texts,
        pii_maps_removed=report.pii_maps,
        files_removed=report.files,
        blobs_removed=report.blobs,
        jobs_removed=report.jobs,
        errors=report.errors,
    )

"""The parse job handler: bytes on disk -> extracted, sectioned text."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.ingest.storage import ObjectStore
from screener_api.ingest.validation import DOCX_MIME, PDF_MIME
from screener_api.models import PiiMap, Resume, ResumeText, StoredFile
from screener_api.outbox.events import EventType, record
from screener_api.parse.extract import (
    Extraction,
    ExtractionError,
    ParseStatus,
    extract_docx,
    extract_pdf,
    needs_ocr,
)
from screener_api.parse.ocr import is_low_confidence, ocr_pdf
from screener_api.parse.sections import detect_language, segment
from screener_api.privacy.redact import redact
from screener_api.queue import JobType, TerminalError, enqueue, idempotency_key
from screener_api.security.crypto import encrypt

log = structlog.get_logger()

PIPELINE_VERSION = "1"


@dataclass(frozen=True)
class ParseOutcome:
    status: ParseStatus
    extraction: Extraction
    sections: dict[str, str]
    language: str | None
    needs_manual_review: bool


def parse_bytes(data: bytes, *, mime: str, ocr_enabled: bool = True) -> ParseOutcome:
    """Pure function: no database, no I/O beyond OCR's subprocess. Directly
    testable against the fixture corpus."""
    if mime == PDF_MIME:
        extraction = extract_pdf(data)
        if ocr_enabled and needs_ocr(extraction):
            log.info("parse.ocr_fallback", chars_per_page=round(extraction.chars_per_page, 1))
            try:
                ocr = ocr_pdf(data)
                # Keep whichever produced more text: OCR on a text-layer PDF can
                # do worse than the text layer itself.
                if len(ocr.text) > len(extraction.text):
                    extraction = ocr
            except ExtractionError as exc:
                log.warning("parse.ocr_failed", error=str(exc))
    elif mime == DOCX_MIME:
        extraction = extract_docx(data)
    else:
        raise TerminalError(f"unsupported mime for parsing: {mime}")

    status = extraction.status
    # LOW_TEXT still has real content worth keeping — it is short, not empty.
    usable = status in (ParseStatus.PARSED, ParseStatus.LOW_TEXT)
    sections = segment(extraction.text) if usable else {}
    language = detect_language(extraction.text) if usable else None

    return ParseOutcome(
        status=status,
        extraction=extraction,
        sections=sections,
        language=language,
        needs_manual_review=is_low_confidence(extraction) or status is ParseStatus.LOW_TEXT,
    )


async def handle_parse_job(
    session: AsyncSession,
    payload: dict[str, object],
    *,
    store: ObjectStore,
    kek: bytes,
    kek_version: int,
) -> None:
    resume_id = uuid.UUID(str(payload["resume_id"]))

    row = (
        await session.execute(
            select(Resume, StoredFile)
            .join(StoredFile, Resume.file_id == StoredFile.id)
            .where(Resume.id == resume_id)
        )
    ).first()
    if row is None:
        # The resume was deleted between enqueue and claim. Nothing to retry.
        raise TerminalError(f"resume {resume_id} no longer exists")

    resume, stored = row
    data = store.get(stored.sha256, org_id=str(resume.org_id))

    try:
        outcome = parse_bytes(data, mime=stored.mime_resolved)
    except ExtractionError as exc:
        resume.parse_status = str(ParseStatus.FAILED)
        resume.parse_error = str(exc)[:200]
        raise TerminalError(str(exc)) from exc

    resume.parse_status = str(outcome.status)
    resume.parse_error = None
    resume.ocr_used = outcome.extraction.ocr_used
    resume.ocr_confidence = outcome.extraction.ocr_confidence
    resume.language = outcome.language
    resume.pipeline_version = PIPELINE_VERSION

    # Upsert, so replaying a job produces the same row rather than a second one.
    existing = (
        await session.execute(select(ResumeText).where(ResumeText.resume_id == resume_id))
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            ResumeText(
                id=uuid.uuid4(),
                org_id=resume.org_id,
                resume_id=resume_id,
                raw_text=outcome.extraction.text,
                char_count=len(outcome.extraction.text),
                sections=dict(outcome.sections),
                extractor=outcome.extraction.extractor,
            )
        )
    else:
        existing.raw_text = outcome.extraction.text
        existing.char_count = len(outcome.extraction.text)
        existing.sections = dict(outcome.sections)
        existing.extractor = outcome.extraction.extractor

    # ---- Redaction happens HERE, inside the network-isolated worker ----------
    # Before anything is embedded, prompted, indexed, or logged. Raw text never
    # leaves this process un-redacted, which is exactly what AC-3 asserts.
    redaction = redact(outcome.extraction.text, header=outcome.sections.get("header"))
    text_row = (
        await session.execute(select(ResumeText).where(ResumeText.resume_id == resume_id))
    ).scalar_one()
    text_row.text_redacted = redaction.text

    envelope = encrypt(
        json.dumps(redaction.token_map, sort_keys=True).encode(),
        kek=kek,
        kek_version=kek_version,
        aad=str(resume.org_id).encode(),
    )
    existing_map = (
        await session.execute(select(PiiMap).where(PiiMap.resume_id == resume_id))
    ).scalar_one_or_none()
    if existing_map is None:
        session.add(
            PiiMap(
                id=uuid.uuid4(),
                org_id=resume.org_id,
                resume_id=resume_id,
                ciphertext=envelope.to_bytes(),
                entity_counts=dict(redaction.counts),
            )
        )
    else:
        existing_map.ciphertext = envelope.to_bytes()
        existing_map.entity_counts = dict(redaction.counts)

    resume.redacted_at = dt.datetime.now(dt.UTC)
    resume.needs_manual_review = outcome.needs_manual_review

    # Chain to embedding in the same transaction: a redacted resume is never
    # left un-indexed, and an embed job never references un-redacted text.
    await enqueue(
        session,
        job_type=JobType.EMBED,
        payload={"resume_id": str(resume_id)},
        key=idempotency_key(
            str(JobType.EMBED),
            input_digest=stored.sha256,
            pipeline_version=PIPELINE_VERSION,
        ),
        org_id=resume.org_id,
    )

    log.info(
        "redaction.completed",
        resume_id=str(resume_id),
        entities=redaction.entity_count,
        counts=redaction.counts,
    )

    # Same transaction as the parse result. If the commit below fails, the
    # notification does not exist either — which is the whole reason this is a
    # table and not an HTTP call (ADR-0018).
    await record(
        session,
        org_id=resume.org_id,
        event_type=EventType.RESUME_PARSED,
        resource_type="resume",
        resource_id=str(resume_id),
        payload={
            "resume_id": str(resume_id),
            "candidate_id": str(resume.candidate_id),
            "parse_status": str(outcome.status),
            "pipeline_version": PIPELINE_VERSION,
        },
        event_key=f"resume.parsed:{resume_id}:{PIPELINE_VERSION}",
    )

    log.info(
        "parse.completed",
        resume_id=str(resume_id),
        status=str(outcome.status),
        extractor=outcome.extraction.extractor,
        chars=len(outcome.extraction.text),
        ocr_used=outcome.extraction.ocr_used,
        language=outcome.language,
    )

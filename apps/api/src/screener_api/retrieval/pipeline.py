"""The embed job: redacted text -> verified chunks -> vectors."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.models import Resume, ResumeChunk, ResumeText
from screener_api.queue import TerminalError
from screener_api.retrieval.chunking import chunk_text
from screener_api.retrieval.embedding import embed_documents

log = structlog.get_logger()

EMBED_BATCH = 32


async def handle_embed_job(session: AsyncSession, payload: dict[str, object]) -> None:
    resume_id = uuid.UUID(str(payload["resume_id"]))

    resume = (
        await session.execute(select(Resume).where(Resume.id == resume_id))
    ).scalar_one_or_none()
    if resume is None:
        raise TerminalError(f"resume {resume_id} no longer exists")

    text_row = (
        await session.execute(select(ResumeText).where(ResumeText.resume_id == resume_id))
    ).scalar_one_or_none()
    if text_row is None or not text_row.text_redacted:
        # Not retryable: redaction has not run, or produced nothing. Retrying
        # cannot change either.
        raise TerminalError(f"resume {resume_id} has no redacted text to embed")

    source = text_row.text_redacted
    sections = {k: str(v) for k, v in (text_row.sections or {}).items()}
    chunks = chunk_text(source, sections=sections)
    if not chunks:
        raise TerminalError(f"resume {resume_id} produced no chunks")

    # The invariant M6 depends on. A chunk whose offsets do not reproduce its own
    # text makes verbatim evidence verification impossible, so fail here rather
    # than let an unverifiable citation reach a recruiter.
    for chunk in chunks:
        if not chunk.verify(source):
            raise TerminalError(
                f"chunk {chunk.index} offsets do not match its text — refusing to index"
            )

    # Replace rather than append: re-running the job must converge on the same
    # rows, not accumulate duplicates.
    await session.execute(delete(ResumeChunk).where(ResumeChunk.resume_id == resume_id))

    vectors: list[list[float]] = []
    for start in range(0, len(chunks), EMBED_BATCH):
        batch = chunks[start : start + EMBED_BATCH]
        vectors.extend(embed_documents([c.text for c in batch]))

    for chunk, vector in zip(chunks, vectors, strict=True):
        session.add(
            ResumeChunk(
                id=uuid.uuid4(),
                org_id=resume.org_id,
                resume_id=resume_id,
                chunk_index=chunk.index,
                text_redacted=chunk.text,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                section=chunk.section,
                embedding=vector,
            )
        )

    log.info(
        "embed.completed",
        resume_id=str(resume_id),
        chunks=len(chunks),
        chars=len(source),
    )

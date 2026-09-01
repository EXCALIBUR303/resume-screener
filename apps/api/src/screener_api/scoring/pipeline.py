"""The score job: everything M0-M6 built, in one place.

    sanitise → retrieve → deterministic → model → verify → fuse → persist

The ordering is load-bearing. Sanitisation runs first so a flagged region
reaches neither the arithmetic nor the prompt (ADR-0012), and verification runs
before fusion so an unverifiable competency contributes zero (ADR-0003).
"""

from __future__ import annotations

import secrets
import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.llm.gateway import LLMGateway, SchemaViolationError
from screener_api.llm.prompts import PromptTemplate
from screener_api.llm.provider import LLMError
from screener_api.models import JobPosting, Match, Resume, ResumeText
from screener_api.outbox.events import EventType
from screener_api.outbox.events import record as record_event
from screener_api.queue import TerminalError
from screener_api.retrieval.search import RRF_K, Hit, hybrid_search
from screener_api.scoring.contracts import MATCH_SCORE_SCHEMA, RubricAssessment
from screener_api.scoring.deterministic import score_deterministic
from screener_api.scoring.evidence import protected_attribute_violations, verify
from screener_api.scoring.fusion import fuse_score
from screener_api.scoring.injection import detect

log = structlog.get_logger()

RETRIEVAL_LIMIT = 8
# A chunk ranked first by BOTH retrievers scores 2/(RRF_K + 1). Normalising by
# that gives a principled 0..1 instead of the magic `* 10` that produced 0.16
# for a perfectly reasonable match.
MAX_RRF = 2.0 / (RRF_K + 1)


def _semantic_score(hits: list[Hit]) -> float:
    """Mean fused rank score of the top chunks, normalised to 0..1."""
    if not hits:
        return 0.0
    top = hits[:5]
    return round(min(1.0, (sum(h.score for h in top) / len(top)) / MAX_RRF), 4)


async def handle_score_job(
    session: AsyncSession,
    payload: dict[str, object],
    *,
    gateway: LLMGateway,
    prompt: PromptTemplate,
    nonce: str | None = None,
) -> None:
    """Score one resume against one job posting.

    ``nonce`` exists for measurement only. The fence around the untrusted
    document is keyed by a per-request random value so an injected instruction
    cannot close it by guessing the delimiter, and that random value goes into
    the prompt — which means two byte-identical resumes produce two different
    prompts and, quite legitimately, two different model responses.

    That is correct in production and fatal in a harness. The counterfactual
    fairness probe measures whether changing a candidate's *name* moves the
    score; with a fresh nonce per call it measured six identical documents
    spreading by 0.34, which swamped every effect it was looking for. Callers
    that need two runs to be comparable pass a fixed value. Nothing in the
    application does, and the default is unchanged.
    """
    job_id = uuid.UUID(str(payload["job_id"]))
    resume_id = uuid.UUID(str(payload["resume_id"]))

    posting = (
        await session.execute(select(JobPosting).where(JobPosting.id == job_id))
    ).scalar_one_or_none()
    resume = (
        await session.execute(select(Resume).where(Resume.id == resume_id))
    ).scalar_one_or_none()
    if posting is None or resume is None:
        raise TerminalError("job posting or resume no longer exists")

    text_row = (
        await session.execute(select(ResumeText).where(ResumeText.resume_id == resume_id))
    ).scalar_one_or_none()
    if text_row is None or not text_row.text_redacted:
        raise TerminalError(f"resume {resume_id} has no redacted text to score")

    # 1. Sanitise BEFORE anything reads the text. See ADR-0012.
    report = detect(text_row.text_redacted)
    document = report.sanitised_text

    # 2. Deterministic terms — computed in Python, from sanitised text only.
    deterministic = score_deterministic(
        document,
        required_skills=list(posting.required_skills),
        nice_to_have=list(posting.nice_to_have),
        min_years=float(posting.min_years),
        hard_requirements=list(posting.hard_requirements),
    )

    # 3. Retrieval, scoped to this tenant and this resume.
    hits = await hybrid_search(
        session,
        org_id=resume.org_id,
        query=posting.description,
        limit=RETRIEVAL_LIMIT,
        resume_ids=[resume_id],
    )
    semantic = _semantic_score(hits)
    sources = {str(h.chunk_id): h.text for h in hits} or {"full": document}

    # 4. The model. Its output is a claim, not a score.
    assessment: RubricAssessment | None = None
    degraded = False
    system, user = prompt.render(
        job_description=posting.description,
        competencies="\n".join(f"- {s}" for s in posting.required_skills),
        resume_id=str(resume_id),
        nonce=nonce or secrets.token_hex(8),
        document="\n\n".join(f"[{cid}] {body}" for cid, body in sources.items()),
    )
    # Provenance comes from the COMPLETION, not from the configured provider.
    # With one provider the two agree; with a router in front of several they do
    # not, and the Match row would name the primary while a fallback wrote the
    # answer. A score is not interpretable without knowing what produced it, so
    # this has to be the model that actually replied (ADR-0019). It falls back
    # to the configured label only on the degraded path, where nothing replied.
    answered_by = gateway.provider.model_id
    try:
        result = gateway.structured(
            system=system,
            user=user,
            model=RubricAssessment,
            schema=MATCH_SCORE_SCHEMA,
        )
        assessment = result.value
        answered_by = result.completion.model_id
    except (SchemaViolationError, LLMError) as exc:
        # Degrade rather than fail: a candidate must not be dropped because our
        # model host was down. The result is flagged and penalised, not hidden.
        log.warning("scoring.degraded", resume_id=str(resume_id), error=str(exc)[:120])
        degraded = True

    if assessment is not None:
        violations = protected_attribute_violations(assessment)
        if violations:
            # The attributes were removed upstream, so this is the model
            # inventing one. Refuse the response outright.
            log.error("scoring.protected_attribute_in_output", names=violations)
            assessment, degraded = None, True

    # 5. Verify every cited quote, per competency.
    verification = verify(assessment, sources=sources) if assessment else None

    # 6. Fuse, exposing every term.
    fused = fuse_score(
        deterministic=deterministic,
        verification=verification,
        semantic_score=semantic,
        degraded=degraded,
        injection_suspected=report.suspected,
        low_ocr_confidence=bool(resume.ocr_used and (resume.ocr_confidence or 100) < 60),
    )

    # 7. Persist with full provenance. Upsert on the uniqueness key so re-running
    #    the same job converges rather than duplicating.
    existing = (
        await session.execute(
            select(Match).where(
                Match.job_id == job_id,
                Match.resume_id == resume_id,
                Match.prompt_version == prompt.version_id,
                Match.model_id == answered_by,
            )
        )
    ).scalar_one_or_none()

    record = existing or Match(
        id=uuid.uuid4(),
        org_id=resume.org_id,
        job_id=job_id,
        resume_id=resume_id,
        model_id=answered_by,
        prompt_version=prompt.version_id,
        prompt_hash=prompt.content_hash,
    )
    record.score = fused.total
    record.components = {
        "contributions": [
            {
                "term": c.term,
                "weight": c.weight,
                "value": c.value,
                "points": c.points,
                "computed_by": c.computed_by,
            }
            for c in fused.contributions
        ],
        "penalties": fused.penalties_applied,
        "explanation": fused.explain(),
        "matched_skills": sorted(deterministic.matched_skills),
        "missing_skills": sorted(deterministic.missing_skills),
        "years_found": deterministic.years_found,
        "hard_gate_failures": deterministic.hard_gate_failures,
        "injection_kinds": report.kinds,
    }
    record.rubric = {
        "competencies": [
            {
                "name": c.name,
                "claimed_level": c.claimed_level,
                "effective_level": c.effective_level,
                "zeroed": c.was_zeroed,
                "quotes_cited": c.quotes_cited,
                "quotes_verified": c.quotes_verified,
            }
            for c in (verification.competencies if verification else [])
        ],
        "aggregate_groundedness": verification.aggregate_groundedness if verification else 0.0,
    }
    record.evidence = {
        c.name: c.verified_quotes
        for c in (verification.competencies if verification else [])
        if c.verified_quotes
    }
    record.unmet_requirements = list(assessment.unmet_requirements) if assessment else []
    record.degraded = fused.degraded
    record.partially_supported = fused.partially_supported
    record.injection_suspected = fused.injection_suspected

    if existing is None:
        session.add(record)

    # The outbox row and the match row commit together. A webhook posted from
    # here instead would announce a score for a transaction that could still
    # roll back, and no retry policy repairs an event for something that never
    # happened (ADR-0018).
    await record_event(
        session,
        org_id=resume.org_id,
        event_type=EventType.RESUME_SCORED,
        resource_type="match",
        resource_id=str(record.id),
        payload={
            "match_id": str(record.id),
            "resume_id": str(resume_id),
            "job_id": str(job_id),
            "score": fused.total,
            "degraded": fused.degraded,
            "partially_supported": fused.partially_supported,
            "injection_suspected": fused.injection_suspected,
            "keyword_stuffing": fused.keyword_stuffing,
            "hard_gate_failures": fused.hard_gate_failures,
            "matched_skill_count": len(deterministic.matched_skills),
            "missing_skill_count": len(deterministic.missing_skills),
            "model_id": answered_by,
            "prompt_version": prompt.version_id,
        },
        event_key=(f"resume.scored:{job_id}:{resume_id}:{prompt.version_id}:{answered_by}"),
    )

    log.info(
        "score.completed",
        resume_id=str(resume_id),
        job_id=str(job_id),
        score=fused.out_of_ten,
        degraded=fused.degraded,
        partially_supported=fused.partially_supported,
        injection_suspected=fused.injection_suspected,
    )

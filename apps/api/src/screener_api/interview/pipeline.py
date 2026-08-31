"""Interview guide generation.

Conditioned on the intersection of what the job needs and what the candidate's
scored evidence does NOT show. Questions about strengths already demonstrated
waste the interview; questions about gaps are the reason the tool exists.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.interview.contracts import INTERVIEW_SCHEMA, InterviewGuide, Question
from screener_api.interview.validation import GuideVerdict, validate_guide
from screener_api.llm.gateway import LLMGateway
from screener_api.llm.prompts import PromptTemplate
from screener_api.models import JobPosting, Match, ResumeText
from screener_api.queue import TerminalError
from screener_api.scoring.injection import detect

log = structlog.get_logger()

DEFAULT_QUESTION_COUNT = 8


@dataclass
class GeneratedGuide:
    questions: list[Question]
    focus_areas: list[str]
    verdict: GuideVerdict
    prompt_version: str
    model_id: str


def competency_summary(match: Match | None, requirements: list[str]) -> str:
    """What the scoring run found, phrased so the model probes the gaps.

    A requirement with no verified evidence is exactly what an interview should
    spend its time on.
    """
    if match is None:
        return "\n".join(f"- {r}: no scoring run yet, evidence unknown" for r in requirements)

    rubric: dict[str, Any] = dict(match.rubric or {})
    competencies: list[dict[str, Any]] = list(rubric.get("competencies", []))
    by_name = {str(c.get("name", "")).lower(): c for c in competencies}
    lines: list[str] = []
    for requirement in requirements:
        found = by_name.get(requirement.lower())
        if found is None:
            lines.append(f"- {requirement}: NOT EVIDENCED — probe this")
        elif int(found.get("effective_level", 0)) == 0:
            lines.append(f"- {requirement}: claimed but unverified — probe this")
        else:
            lines.append(f"- {requirement}: evidenced at level {found.get('effective_level')} of 4")
    return "\n".join(lines)


async def generate_guide(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    resume_id: uuid.UUID,
    gateway: LLMGateway,
    prompt: PromptTemplate,
    count: int = DEFAULT_QUESTION_COUNT,
) -> GeneratedGuide:
    posting = (
        await session.execute(select(JobPosting).where(JobPosting.id == job_id))
    ).scalar_one_or_none()
    text_row = (
        await session.execute(select(ResumeText).where(ResumeText.resume_id == resume_id))
    ).scalar_one_or_none()
    if posting is None or text_row is None or not text_row.text_redacted:
        raise TerminalError("job posting or redacted resume text is missing")

    match = (
        await session.execute(
            select(Match)
            .where(Match.job_id == job_id, Match.resume_id == resume_id)
            .order_by(Match.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    # Same sanitisation as scoring: a document that tries to steer the model
    # must not get to steer the interview either.
    document = detect(text_row.text_redacted).sanitised_text
    requirements = list(posting.required_skills)

    system, user = prompt.render(
        job_description=posting.description,
        competency_summary=competency_summary(match, requirements),
        resume_id=str(resume_id),
        nonce=secrets.token_hex(8),
        document=document,
        count=count,
    )
    guide = gateway.structured(
        system=system,
        user=user,
        model=InterviewGuide,
        schema=INTERVIEW_SCHEMA,
        temperature=0.3,
    ).value

    verdict = validate_guide(guide, requirements=requirements, document=document)
    log.info(
        "interview.guide_generated",
        job_id=str(job_id),
        resume_id=str(resume_id),
        proposed=len(guide.questions),
        accepted=len(verdict.accepted),
        rejected=len(verdict.rejected),
    )
    return GeneratedGuide(
        questions=verdict.accepted,
        focus_areas=list(guide.focus_areas),
        verdict=verdict,
        prompt_version=prompt.version_id,
        model_id=gateway.provider.model_id,
    )

"""Interview guide generation, on demand."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.db import get_session
from screener_api.interview.pipeline import generate_guide
from screener_api.llm.factory import build_gateway
from screener_api.llm.gateway import SchemaViolationError
from screener_api.llm.prompts import active_version, load
from screener_api.llm.provider import LLMError
from screener_api.queue import TerminalError
from screener_api.security import audit
from screener_api.security.deps import Actor, requires
from screener_api.security.roles import Permission
from screener_api.settings import Settings, get_settings

router = APIRouter(prefix="/interviews", tags=["interviews"])


class QuestionOut(BaseModel):
    question: str
    competency: str
    difficulty: str
    probe_reason: str
    cites_requirement: str | None
    cites_evidence: str | None
    rubric: list[dict[str, object]]


class GuideOut(BaseModel):
    job_id: uuid.UUID
    resume_id: uuid.UUID
    questions: list[QuestionOut]
    focus_areas: list[str]
    # Transparency about what the model proposed versus what survived
    # validation. A guide that silently drops half its questions should say so.
    proposed: int
    accepted: int
    rejected_reasons: list[str]
    model_id: str
    prompt_version: str


@router.post("/{job_id}/{resume_id}", response_model=GuideOut)
async def create_guide(
    job_id: uuid.UUID,
    resume_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    actor: Annotated[Actor, requires(Permission.INTERVIEW_WRITE)],
    count: int = 8,
) -> GuideOut:
    prompt = load(
        "interview_questions",
        active_version("interview_questions", settings.llm_prompt_version),
    )
    try:
        guide = await generate_guide(
            session,
            job_id=job_id,
            resume_id=resume_id,
            gateway=build_gateway(settings),
            prompt=prompt,
            count=max(1, min(count, 12)),
        )
    except TerminalError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (SchemaViolationError, LLMError) as exc:
        # A model that will not produce valid output is an upstream failure, not
        # a bug in this service. 502 says so; a bare 500 would send an operator
        # hunting through our own stack traces.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "The language model did not return a usable interview guide. Retry shortly.",
        ) from exc

    await audit.record(
        session,
        action="interview.guide_generated",
        resource_type="resume",
        resource_id=str(resume_id),
        org_id=actor.org_id,
        actor_user_id=actor.user_id,
        actor_ip=request.client.host if request.client else None,
        meta={
            "job_id": str(job_id),
            "accepted": len(guide.questions),
            "rejected": len(guide.verdict.rejected),
        },
    )
    await session.commit()

    return GuideOut(
        job_id=job_id,
        resume_id=resume_id,
        questions=[
            QuestionOut(
                question=q.question,
                competency=q.competency,
                difficulty=q.difficulty,
                probe_reason=q.probe_reason,
                cites_requirement=q.cites_requirement,
                cites_evidence=q.cites_evidence,
                rubric=[{"level": a.level, "descriptor": a.descriptor} for a in q.rubric],
            )
            for q in guide.questions
        ],
        focus_areas=guide.focus_areas,
        proposed=len(guide.verdict.verdicts),
        accepted=len(guide.questions),
        rejected_reasons=[v.banned_reason or "ungrounded" for v in guide.verdict.rejected],
        model_id=guide.model_id,
        prompt_version=guide.prompt_version,
    )

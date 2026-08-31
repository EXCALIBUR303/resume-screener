"""Job postings and ranked candidate lists — the recruiter-facing surface."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.db import get_session
from screener_api.models import Candidate, JobPosting, Match, Resume
from screener_api.queue import JobType, enqueue, idempotency_key
from screener_api.security import audit
from screener_api.security.deps import Actor, requires
from screener_api.security.roles import Permission

router = APIRouter(prefix="/jobs", tags=["jobs"])
log = structlog.get_logger()


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=10, max_length=20_000)
    required_skills: list[str] = Field(default_factory=list, max_length=40)
    nice_to_have: list[str] = Field(default_factory=list, max_length=40)
    hard_requirements: list[str] = Field(default_factory=list, max_length=10)
    min_years: int = Field(default=0, ge=0, le=50)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    required_skills: list[str]
    min_years: int
    status: str


class MatchOut(BaseModel):
    """A ranked candidate, with everything needed to defend the number."""

    resume_id: uuid.UUID
    candidate_id: uuid.UUID
    pseudonym: str
    score: float
    score_out_of_ten: float
    contributions: list[dict[str, Any]]
    penalties: dict[str, float]
    competencies: list[dict[str, Any]]
    evidence: dict[str, list[str]]
    unmet_requirements: list[str]
    matched_skills: list[str]
    missing_skills: list[str]
    # Never show a number without showing how much of it we can defend.
    degraded: bool
    partially_supported: bool
    injection_suspected: bool
    model_id: str
    prompt_version: str


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[Actor, requires(Permission.JOB_WRITE)],
) -> JobOut:
    posting = JobPosting(
        id=uuid.uuid4(),
        org_id=actor.org_id,
        title=body.title,
        description=body.description,
        required_skills=body.required_skills,
        nice_to_have=body.nice_to_have,
        hard_requirements=body.hard_requirements,
        min_years=body.min_years,
        created_by=actor.user_id,
    )
    session.add(posting)
    await audit.record(
        session,
        action="job.created",
        resource_type="job_posting",
        resource_id=str(posting.id),
        org_id=actor.org_id,
        actor_user_id=actor.user_id,
        actor_ip=request.client.host if request.client else None,
        meta={"title": body.title, "required_skills": len(body.required_skills)},
    )
    await session.commit()
    return JobOut.model_validate(posting)


@router.get("", response_model=list[JobOut])
async def list_jobs(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[Actor, requires(Permission.JOB_READ)],
) -> list[JobOut]:
    postings = (
        (
            await session.execute(
                select(JobPosting)
                .where(JobPosting.org_id == actor.org_id)
                .order_by(JobPosting.created_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return [JobOut.model_validate(p) for p in postings]


@router.post("/{job_id}/score", status_code=status.HTTP_202_ACCEPTED)
async def score_all_candidates(
    job_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[Actor, requires(Permission.JOB_WRITE)],
) -> dict[str, int]:
    """Queue every parsed resume in this organisation against this role."""
    posting = (
        await session.execute(
            select(JobPosting).where(JobPosting.id == job_id, JobPosting.org_id == actor.org_id)
        )
    ).scalar_one_or_none()
    if posting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    resumes = (
        (
            await session.execute(
                select(Resume).where(
                    Resume.org_id == actor.org_id,
                    Resume.parse_status.in_(("parsed", "low_text")),
                )
            )
        )
        .scalars()
        .all()
    )

    queued = 0
    for resume in resumes:
        created = await enqueue(
            session,
            job_type=JobType.SCORE,
            payload={"job_id": str(job_id), "resume_id": str(resume.id)},
            key=idempotency_key(
                str(JobType.SCORE),
                input_digest=f"{job_id}:{resume.id}",
                pipeline_version="1",
            ),
            org_id=actor.org_id,
        )
        queued += created is not None

    await audit.record(
        session,
        action="job.scoring_requested",
        resource_type="job_posting",
        resource_id=str(job_id),
        org_id=actor.org_id,
        actor_user_id=actor.user_id,
        actor_ip=request.client.host if request.client else None,
        meta={"candidates": len(resumes), "queued": queued},
    )
    await session.commit()
    return {"candidates": len(resumes), "queued": queued}


@router.get("/{job_id}/matches", response_model=list[MatchOut])
async def ranked_matches(
    job_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[Actor, requires(Permission.MATCH_READ)],
    limit: int = 50,
) -> list[MatchOut]:
    """The ranked list. This is the product."""
    rows = (
        await session.execute(
            select(Match, Candidate)
            .join(Resume, Resume.id == Match.resume_id)
            .join(Candidate, Candidate.id == Resume.candidate_id)
            .where(Match.job_id == job_id, Match.org_id == actor.org_id)
            .order_by(Match.score.desc())
            .limit(min(limit, 200))
        )
    ).all()

    out: list[MatchOut] = []
    for match, candidate in rows:
        components = dict(match.components or {})
        out.append(
            MatchOut(
                resume_id=match.resume_id,
                candidate_id=candidate.id,
                pseudonym=candidate.pseudonym,
                score=match.score,
                score_out_of_ten=round(match.score * 10, 2),
                contributions=list(components.get("contributions", [])),
                penalties=dict(components.get("penalties", {})),
                competencies=list((match.rubric or {}).get("competencies", [])),
                evidence=dict(match.evidence or {}),
                unmet_requirements=list(match.unmet_requirements or []),
                matched_skills=list(components.get("matched_skills", [])),
                missing_skills=list(components.get("missing_skills", [])),
                degraded=match.degraded,
                partially_supported=match.partially_supported,
                injection_suspected=match.injection_suspected,
                model_id=match.model_id,
                prompt_version=match.prompt_version,
            )
        )
    return out

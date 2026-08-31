"""Verbatim evidence verification — the anti-hallucination mechanism.

ADR-0003 measured this. `qwen3:8b` obeyed a prompt-injected resume completely:
every competency set to 4, a fabricated Kubernetes citation, an emptied
``unmet_requirements``. The fence, the nonce and the system instruction did not
stop it. What stopped it was checking each cited quote against the source.

The granularity is **per competency**, never aggregate. On that run the aggregate
groundedness was 83% — five real spans masking one fabricated one. On a longer
resume with twenty spans a single fabrication scores ~95% and sails through an
aggregate gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

from screener_api.scoring.contracts import Competency, RubricAssessment

log = structlog.get_logger()

_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Whitespace-insensitive comparison. A model reflowing a line is not a
    fabrication; inventing one is."""
    return _WHITESPACE.sub(" ", text).strip().lower()


@dataclass
class VerifiedCompetency:
    name: str
    claimed_level: int
    effective_level: int
    quotes_cited: int
    quotes_verified: int
    verified_quotes: list[str] = field(default_factory=list)

    @property
    def was_zeroed(self) -> bool:
        return self.effective_level < self.claimed_level


@dataclass
class VerificationResult:
    competencies: list[VerifiedCompetency]
    partially_supported: bool
    aggregate_groundedness: float

    @property
    def mean_effective_level(self) -> float:
        if not self.competencies:
            return 0.0
        return sum(c.effective_level for c in self.competencies) / len(self.competencies)


def verify(assessment: RubricAssessment, *, sources: dict[str, str]) -> VerificationResult:
    """Gate every competency independently against the text it cites.

    A competency with zero verifiable quotes contributes ``level = 0``, whatever
    the model claimed.
    """
    haystacks = {cid: normalise(text) for cid, text in sources.items()}
    combined = " ".join(haystacks.values())

    verified: list[VerifiedCompetency] = []
    total_quotes = total_verified = 0

    for competency in assessment.competencies:
        good: list[str] = []
        for item in competency.evidence:
            total_quotes += 1
            needle = normalise(item.quote)
            # Prefer the chunk the model named; fall back to the whole document,
            # because citing the wrong chunk id is sloppiness, not fabrication.
            target = haystacks.get(item.chunk_id, combined)
            if needle and (needle in target or needle in combined):
                good.append(item.quote)
                total_verified += 1

        verified.append(
            VerifiedCompetency(
                name=competency.name,
                claimed_level=competency.level,
                effective_level=competency.level if good else 0,
                quotes_cited=len(competency.evidence),
                quotes_verified=len(good),
                verified_quotes=good,
            )
        )

    partially = any(v.was_zeroed for v in verified)
    if partially:
        log.warning(
            "scoring.unverified_evidence_dropped",
            zeroed=[v.name for v in verified if v.was_zeroed],
        )

    return VerificationResult(
        competencies=verified,
        partially_supported=partially,
        aggregate_groundedness=(total_verified / total_quotes) if total_quotes else 0.0,
    )


def unsupported_names(assessment: RubricAssessment) -> list[str]:
    """Competencies claimed at a level above zero while citing nothing at all."""
    return [c.name for c in assessment.competencies if c.level > 0 and not c.evidence]


def _competency_is_protected(competency: Competency) -> bool:
    """Protected attributes must never appear in a competency name.

    They were removed before the model saw anything, so this catches a model
    inventing one rather than reading it.
    """
    banned = (
        "gender",
        "male",
        "female",
        "age",
        "married",
        "religion",
        "race",
        "ethnic",
        "nationality",
        "disability",
        "pregnan",
        "caste",
    )
    lowered = competency.name.lower()
    return any(term in lowered for term in banned)


def protected_attribute_violations(assessment: RubricAssessment) -> list[str]:
    return [c.name for c in assessment.competencies if _competency_is_protected(c)]

"""Question validation — the rules that make output non-generic and lawful.

Two independent gates:

* **Grounding.** A question must cite a job requirement or a verbatim resume
  span. This is what rejects "tell me about yourself", and it is checked the
  same way evidence is checked in scoring: the quote must actually be there.
* **Lawfulness.** Protected attributes and personal-circumstance questions are
  rejected outright. They were removed before the model saw the document, so a
  reference to one is the model inventing it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

from screener_api.interview.contracts import InterviewGuide, Question
from screener_api.scoring.evidence import normalise

log = structlog.get_logger()

# Questions that are unlawful or unwise in most jurisdictions, plus the ones a
# screening tool has no business asking at all.
BANNED_TOPICS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        # Stems take \w* rather than a trailing \b: `disabilit\b` cannot match
        # "disability", because y is a word character - the same class of bug as
        # the 100% boundary in the injection detector. Age proxies are listed
        # explicitly because "younger candidates" never contains the word "age".
        "protected_attribute",
        re.compile(
            r"\b(?:ages?|aged|how old|young(?:er|est)?|old(?:er|est)?"
            r"|gender\w*|male|female|married|marital|spouse|child(?:ren)?"
            r"|pregnan\w*|religio\w*|race|racial|ethnic\w*|nationalit\w*"
            r"|citizenship|visa|disabilit\w*|disabled|health condition"
            r"|sexual orientation)\b",
            re.I,
        ),
    ),
    (
        "personal_circumstances",
        re.compile(
            r"\b(?:salary history|current salary|notice period|why did you leave|"
            r"family|childcare|living arrangement|commute)\b",
            re.I,
        ),
    ),
)

GENERIC = re.compile(
    r"^\s*(?:tell me about yourself|what are your strengths|"
    r"what are your weaknesses|where do you see yourself|why should we hire you)",
    re.I,
)


@dataclass
class QuestionVerdict:
    question: Question
    grounded: bool
    evidence_verified: bool
    banned_reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.grounded and self.banned_reason is None


@dataclass
class GuideVerdict:
    verdicts: list[QuestionVerdict] = field(default_factory=list)

    @property
    def accepted(self) -> list[Question]:
        return [v.question for v in self.verdicts if v.accepted]

    @property
    def rejected(self) -> list[QuestionVerdict]:
        return [v for v in self.verdicts if not v.accepted]

    @property
    def acceptance_rate(self) -> float:
        return len(self.accepted) / len(self.verdicts) if self.verdicts else 0.0


def validate_guide(
    guide: InterviewGuide, *, requirements: list[str], document: str
) -> GuideVerdict:
    haystack = normalise(document)
    required = {r.strip().lower() for r in requirements if r.strip()}

    verdicts: list[QuestionVerdict] = []
    for question in guide.questions:
        banned = _banned_reason(question)

        cites_req = bool(
            question.cites_requirement and question.cites_requirement.strip().lower() in required
        )
        quote = (question.cites_evidence or "").strip()
        # Verified the same way scoring verifies evidence: the quote must be in
        # the document, not merely plausible.
        verified = bool(quote) and normalise(quote) in haystack

        # A question about a MISSING requirement legitimately cites the
        # requirement and no evidence — that is the gap-probing case, and it is
        # the most useful kind of question there is.
        grounded = cites_req or verified
        if GENERIC.match(question.question):
            grounded = False

        verdicts.append(
            QuestionVerdict(
                question=question,
                grounded=grounded,
                evidence_verified=verified,
                banned_reason=banned,
            )
        )

    verdict = GuideVerdict(verdicts=verdicts)
    if verdict.rejected:
        log.warning(
            "interview.questions_rejected",
            count=len(verdict.rejected),
            reasons=[v.banned_reason or "ungrounded" for v in verdict.rejected],
        )
    return verdict


def _banned_reason(question: Question) -> str | None:
    haystack = " ".join(
        [
            question.question,
            question.competency,
            question.probe_reason,
            *(a.descriptor for a in question.rubric),
        ]
    )
    for label, pattern in BANNED_TOPICS:
        if pattern.search(haystack):
            return label
    return None

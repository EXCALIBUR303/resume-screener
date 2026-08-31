"""Interview guide validation: grounding and lawfulness."""

from __future__ import annotations

from typing import ClassVar

import pytest

from screener_api.interview.contracts import InterviewGuide
from screener_api.interview.validation import validate_guide

DOCUMENT = (
    "PERSON_1\n\nWORK EXPERIENCE\n"
    "Senior Backend Engineer, ORG_1 (2021-2026)\n"
    "Designed payment services in Python on PostgreSQL at high throughput.\n"
    "Led the migration from a monolith to six services.\n\n"
    "TECHNICAL SKILLS\nPython, PostgreSQL, Docker\n"
)
REQUIREMENTS = ["Python", "PostgreSQL", "Kubernetes"]


def _guide(*questions: dict) -> InterviewGuide:
    return InterviewGuide.model_validate(
        {
            "questions": list(questions),
            "focus_areas": ["reliability"],
        }
    )


def _q(**over) -> dict:
    base = {
        "question": "Walk me through how you designed the payment service schema.",
        "competency": "PostgreSQL",
        "difficulty": "core",
        "probe_reason": "Evidenced at level 3; probe depth.",
        "cites_requirement": "PostgreSQL",
        "cites_evidence": "Designed payment services in Python on PostgreSQL",
        "rubric": [
            {"level": 1, "descriptor": "Cannot describe the schema at all."},
            {"level": 3, "descriptor": "Describes tables and access patterns."},
            {"level": 5, "descriptor": "Explains tradeoffs and failure modes."},
        ],
    }
    base.update(over)
    return base


def test_a_grounded_question_is_accepted() -> None:
    verdict = validate_guide(_guide(_q()), requirements=REQUIREMENTS, document=DOCUMENT)
    assert verdict.acceptance_rate == 1.0
    assert verdict.verdicts[0].evidence_verified


def test_a_question_citing_nothing_is_rejected() -> None:
    """The rule that makes output non-generic."""
    verdict = validate_guide(
        _guide(_q(cites_requirement=None, cites_evidence=None)),
        requirements=REQUIREMENTS,
        document=DOCUMENT,
    )
    assert not verdict.accepted


@pytest.mark.parametrize(
    "text",
    [
        "Tell me about yourself.",
        "What are your strengths and weaknesses in this role?",
        "Where do you see yourself in five years from now?",
    ],
)
def test_generic_questions_are_rejected_even_when_they_cite(text: str) -> None:
    """Citing a requirement must not launder a filler question."""
    verdict = validate_guide(
        _guide(_q(question=text)), requirements=REQUIREMENTS, document=DOCUMENT
    )
    assert not verdict.accepted


def test_a_fabricated_quote_does_not_ground_a_question() -> None:
    """Evidence is verified the same way scoring verifies it."""
    verdict = validate_guide(
        _guide(
            _q(
                cites_requirement=None,
                cites_evidence="Ran Kubernetes clusters across three regions",
            )
        ),
        requirements=REQUIREMENTS,
        document=DOCUMENT,
    )
    assert not verdict.accepted
    assert not verdict.verdicts[0].evidence_verified


def test_a_gap_question_grounds_on_the_requirement_alone() -> None:
    """The most useful question type: a requirement with NO evidence. It cites
    the requirement and nothing from the document, and that is correct."""
    verdict = validate_guide(
        _guide(
            _q(
                question="How have you handled rolling deploys on Kubernetes?",
                competency="Kubernetes",
                cites_requirement="Kubernetes",
                cites_evidence=None,
                probe_reason="Not evidenced anywhere in the resume.",
            )
        ),
        requirements=REQUIREMENTS,
        document=DOCUMENT,
    )
    assert verdict.acceptance_rate == 1.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("question", "How old are you and do you have children?"),
        ("question", "What is your current salary and notice period?"),
        ("competency", "Cultural fit and marital status"),
        ("probe_reason", "Candidate appears to have a disability."),
    ],
)
def test_unlawful_questions_are_rejected(field: str, value: str) -> None:
    verdict = validate_guide(
        _guide(_q(**{field: value})), requirements=REQUIREMENTS, document=DOCUMENT
    )
    assert not verdict.accepted
    assert verdict.verdicts[0].banned_reason is not None


def test_a_banned_rubric_anchor_rejects_the_question() -> None:
    """The rubric is part of the question. An anchor that describes who someone
    IS rather than what they demonstrate is the same failure."""
    verdict = validate_guide(
        _guide(
            _q(rubric=[{"level": 3, "descriptor": "Younger candidates tend to answer faster."}])
        ),
        requirements=REQUIREMENTS,
        document=DOCUMENT,
    )
    assert not verdict.accepted


def test_mixed_guides_keep_only_the_good_questions() -> None:
    verdict = validate_guide(
        _guide(
            _q(),
            _q(question="Tell me about yourself."),
            _q(
                competency="Kubernetes",
                cites_requirement="Kubernetes",
                cites_evidence=None,
                question="Describe your experience running Kubernetes in production.",
            ),
        ),
        requirements=REQUIREMENTS,
        document=DOCUMENT,
    )
    assert len(verdict.accepted) == 2
    assert len(verdict.rejected) == 1


def test_competency_summary_names_the_gaps() -> None:
    from screener_api.interview.pipeline import competency_summary

    class _Match:
        rubric: ClassVar[dict] = {
            "competencies": [
                {"name": "Python", "effective_level": 3},
                {"name": "Kubernetes", "effective_level": 0},
            ]
        }

    summary = competency_summary(_Match(), REQUIREMENTS)  # type: ignore[arg-type]
    assert "Python: evidenced at level 3" in summary
    assert "Kubernetes: claimed but unverified" in summary
    assert "PostgreSQL: NOT EVIDENCED" in summary

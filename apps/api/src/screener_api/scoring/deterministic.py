"""The half of the score a prompt injection cannot touch.

Skill overlap, years of experience and hard gates are computed in Python from
the source text. An injected resume can tell the model anything; it cannot make
this arithmetic produce a different number.

ADR-0003 measured why that matters: the model set every competency to 4 and
claimed Kubernetes it did not have. The deterministic terms did not move.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Aliases so "Postgres" and "PostgreSQL" are one skill. A committed table rather
# than a fuzzy match: reviewable, testable, and it cannot drift silently.
ALIASES: dict[str, str] = {
    "postgres": "postgresql",
    "psql": "postgresql",
    "pg": "postgresql",
    "k8s": "kubernetes",
    "kube": "kubernetes",
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "golang": "go",
    "node": "node.js",
    "nodejs": "node.js",
    "aws lambda": "aws",
    "amazon web services": "aws",
    "gcp": "google cloud",
    "ml": "machine learning",
    "ci/cd": "cicd",
    "ci cd": "cicd",
    "rest api": "rest",
    "restful": "rest",
    "postgre": "postgresql",
    "tensorflow2": "tensorflow",
}

_TOKEN = re.compile(r"[a-z0-9+#.]+(?:\s[a-z0-9+#.]+)?")
# Resumes write "seven years" as readily as "7 years". Missing the spelled-out
# form silently under-counts experience for a whole class of candidates.
_WORD_NUMBERS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
}

_YEARS = re.compile(
    r"(?:(\d{1,2})|(" + "|".join(_WORD_NUMBERS) + r"))\s*\+?\s*(?:years?|yrs?)\b",
    re.I,
)
_DATE_RANGE = re.compile(
    # Escapes rather than literal dashes: resumes use en and em dashes for
    # date ranges, and a literal one here is invisible in review.
    r"\b(19[89]\d|20[0-4]\d)\s*[-\u2013\u2014]?\s*(?:to\s*)?(19[89]\d|20[0-4]\d|present)\b",
    re.I,
)


def canonical(skill: str) -> str:
    cleaned = skill.strip().lower().strip(".,;:")
    return ALIASES.get(cleaned, cleaned)


def extract_skills(text: str, *, vocabulary: set[str]) -> set[str]:
    """Find which of a known vocabulary appear in the text.

    Vocabulary-driven rather than open-ended: the job description defines what
    matters, so there is no need to guess at what a token means.
    """
    lowered = text.lower()
    found: set[str] = set()
    for skill in vocabulary:
        needle = canonical(skill)
        # Word-boundary match so "go" does not match "going" and "r" does not
        # match every word containing it.
        if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", lowered):
            found.add(needle)
        for alias, target in ALIASES.items():
            if target == needle and re.search(
                rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered
            ):
                found.add(needle)
    return found


def estimate_years(text: str) -> float:
    """Best-effort years of experience.

    Two signals: an explicit claim ("seven years", "7+ years") and the span of
    employment date ranges. The larger is taken, since resumes under-state as
    often as they over-state.
    """
    claimed: list[int] = []
    for match in _YEARS.finditer(text):
        digits, word = match.group(1), match.group(2)
        if digits:
            claimed.append(int(digits))
        elif word:
            claimed.append(_WORD_NUMBERS[word.lower()])
    explicit = max(claimed) if claimed else 0.0

    spans = 0.0
    for match in _DATE_RANGE.finditer(text):
        start = int(match.group(1))
        end_raw = match.group(2).lower()
        end = 2026 if end_raw == "present" else int(end_raw)
        if end >= start:
            spans = max(spans, float(end - start))

    return float(max(explicit, spans))


@dataclass
class DeterministicScore:
    skill_score: float
    experience_score: float
    matched_skills: set[str] = field(default_factory=set)
    missing_skills: set[str] = field(default_factory=set)
    years_found: float = 0.0
    years_required: float = 0.0
    hard_gate_failures: list[str] = field(default_factory=list)

    @property
    def passes_hard_gates(self) -> bool:
        return not self.hard_gate_failures


def score_deterministic(
    resume_text: str,
    *,
    required_skills: list[str],
    nice_to_have: list[str] | None = None,
    min_years: float = 0.0,
    hard_requirements: list[str] | None = None,
) -> DeterministicScore:
    required = {canonical(s) for s in required_skills if s.strip()}
    optional = {canonical(s) for s in (nice_to_have or []) if s.strip()}

    present = extract_skills(resume_text, vocabulary=required | optional)
    matched_required = present & required
    matched_optional = present & optional

    # Required skills carry full weight; nice-to-haves add a capped bonus so a
    # candidate cannot compensate for missing essentials with extras.
    base = len(matched_required) / len(required) if required else 1.0
    bonus = 0.1 * (len(matched_optional) / len(optional)) if optional else 0.0
    skill_score = min(1.0, base + bonus)

    years = estimate_years(resume_text)
    # Linear up to the requirement, then flat: twice the required experience is
    # not twice as good.
    experience = 1.0 if min_years <= 0 else min(1.0, years / min_years)

    failures: list[str] = []
    for requirement in hard_requirements or []:
        if canonical(requirement) not in present:
            failures.append(requirement)

    return DeterministicScore(
        skill_score=round(skill_score, 4),
        experience_score=round(experience, 4),
        matched_skills=matched_required | matched_optional,
        missing_skills=required - matched_required,
        years_found=years,
        years_required=min_years,
        hard_gate_failures=failures,
    )

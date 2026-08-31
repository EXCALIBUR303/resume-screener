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

from screener_api.privacy.recognizers import NEVER_REDACT

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


# A skills list is weak evidence; a sentence describing the work is strong.
# Below this word count a line is an enumeration, not a claim with substance.
SUPPORTING_LINE_WORDS = 8
# Words that assert competence without describing any work. "Expert in
# Kubernetes. Expert in PostgreSQL." is nine words with low technology density,
# so a density test alone accepted it as evidence. Evidence says what you DID.
_CLAIM_FILLER = frozenset(
    {
        "expert",
        "expertise",
        "proficient",
        "proficiency",
        "skilled",
        "skills",
        "experienced",
        "experience",
        "knowledge",
        "knowledgeable",
        "familiar",
        "familiarity",
        "strong",
        "solid",
        "excellent",
        "advanced",
        "in",
        "with",
        "of",
        "and",
        "or",
        "the",
        "a",
        "an",
        "at",
        "on",
        "for",
        "to",
        "years",
        "technologies",
        "technology",
        "stack",
        "tools",
        "keywords",
        "including",
    }
)
# A line needs at least this many words that are neither technology names nor
# competence-claim filler before it counts as describing real work.
MIN_SUBSTANTIVE_WORDS = 3
# What a skill is worth when it is only named, never demonstrated. Not zero:
# listing a skill is a real (if weak) signal, and a resume that lists honestly
# should not be treated as an attack.
NAMED_ONLY_CREDIT = 0.35
# Above this share of named-but-unevidenced skills, the document looks stuffed.
STUFFING_RATIO = 0.6


def _supporting_lines(text: str) -> list[str]:
    """Lines with enough substance to count as evidence rather than enumeration."""
    out: list[str] = []
    for line in text.split("\n"):
        words = line.split()
        if len(words) < SUPPORTING_LINE_WORDS:
            continue
        # A long comma-separated list is still a list.
        if line.count(",") >= max(3, len(words) // 3):
            continue
        # So is a space-separated one. "Proficient: Kubernetes PostgreSQL Python
        # Docker Redis Kafka Terraform" is eight words and no commas, so a
        # word-count test called it a sentence. A line whose tokens are mostly
        # technology names is an enumeration whatever separates them.
        tokens = [w.strip(".,;:()").lower() for w in words]
        meaningful = [t for t in tokens if len(t) > 1]
        if meaningful:
            tech = sum(1 for t in meaningful if t in NEVER_REDACT)
            if tech / len(meaningful) >= 0.5:
                continue
        substantive = [t for t in meaningful if t not in NEVER_REDACT and t not in _CLAIM_FILLER]
        if len(substantive) < MIN_SUBSTANTIVE_WORDS:
            continue
        out.append(line.lower())
    return out


def skill_support(text: str, skills: set[str]) -> dict[str, bool]:
    """Which skills appear in a sentence with substance, not just a list.

    Bare substring matching credited "Kubernetes Kubernetes Kubernetes" exactly
    as much as "Ran workloads on Kubernetes, owning rollouts and autoscaling".
    All six keyword-stuffing cases in the AC-9 corpus raised the score because of
    it, undetected — stuffing carries no instruction language, so sanitisation
    never fires. See ADR-0014.
    """
    supporting = _supporting_lines(text)
    result: dict[str, bool] = {}
    for skill in skills:
        needle = canonical(skill)
        aliases = {needle} | {a for a, t in ALIASES.items() if t == needle}
        result[skill] = any(
            re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", line)
            for line in supporting
            for a in aliases
        )
    return result


@dataclass
class DeterministicScore:
    skill_score: float
    experience_score: float
    matched_skills: set[str] = field(default_factory=set)
    missing_skills: set[str] = field(default_factory=set)
    years_found: float = 0.0
    years_required: float = 0.0
    hard_gate_failures: list[str] = field(default_factory=list)
    evidenced_skills: set[str] = field(default_factory=set)
    named_only_skills: set[str] = field(default_factory=set)
    looks_stuffed: bool = False

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

    # A named skill is worth less than a demonstrated one. This is what stops
    # keyword stuffing from paying, and it needs no detector to fire.
    support = skill_support(resume_text, matched_required | matched_optional)
    evidenced = {s for s, ok in support.items() if ok}
    named_only = (matched_required | matched_optional) - evidenced

    def credit(skills: set[str]) -> float:
        return sum(1.0 if s in evidenced else NAMED_ONLY_CREDIT for s in skills)

    # Required skills carry full weight; nice-to-haves add a capped bonus so a
    # candidate cannot compensate for missing essentials with extras.
    base = credit(matched_required) / len(required) if required else 1.0
    bonus = 0.1 * (credit(matched_optional) / len(optional)) if optional else 0.0
    skill_score = min(1.0, base + bonus)

    years = estimate_years(resume_text)
    # Linear up to the requirement, then flat: twice the required experience is
    # not twice as good.
    experience = 1.0 if min_years <= 0 else min(1.0, years / min_years)

    failures: list[str] = []
    for requirement in hard_requirements or []:
        if canonical(requirement) not in present:
            failures.append(requirement)

    total_named = len(matched_required | matched_optional)
    stuffing = bool(total_named) and (len(named_only) / total_named) >= STUFFING_RATIO

    return DeterministicScore(
        skill_score=round(skill_score, 4),
        evidenced_skills=evidenced,
        named_only_skills=named_only,
        looks_stuffed=stuffing,
        experience_score=round(experience, 4),
        matched_skills=matched_required | matched_optional,
        missing_skills=required - matched_required,
        years_found=years,
        years_required=min_years,
        hard_gate_failures=failures,
    )

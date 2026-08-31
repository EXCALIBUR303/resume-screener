"""Section segmentation and language detection.

Sections matter downstream for two reasons: retrieval can weight experience
above hobbies, and the career-changer case (relevant work sitting under
"Projects" rather than "Experience") is one the golden set deliberately
includes.
"""

from __future__ import annotations

import re
from typing import Final

# Ordered by specificity: "work experience" must win over a bare "experience".
HEADINGS: Final[dict[str, tuple[str, ...]]] = {
    "summary": (
        "professional summary",
        "career summary",
        "summary",
        "objective",
        "profile",
        "about me",
    ),
    "experience": (
        "work experience",
        "professional experience",
        "employment history",
        "experience",
        "employment",
        "career history",
        "work history",
    ),
    "education": ("education", "academic background", "qualifications", "academics"),
    "skills": (
        "technical skills",
        "core competencies",
        "skills",
        "technologies",
        "tech stack",
        "competencies",
    ),
    "projects": (
        "projects",
        "personal projects",
        "side projects",
        "portfolio",
        "selected projects",
    ),
    "certifications": ("certifications", "certificates", "licenses", "accreditations"),
    "publications": ("publications", "papers", "research"),
    "awards": ("awards", "honors", "honours", "achievements"),
    "languages": ("languages",),
    "interests": ("interests", "hobbies", "activities"),
    "references": ("references",),
}

_ALL: Final[list[tuple[str, str]]] = sorted(
    ((section, heading) for section, headings in HEADINGS.items() for heading in headings),
    key=lambda pair: len(pair[1]),
    reverse=True,
)

# A heading line is short, mostly letters, and not a sentence.
_MAX_HEADING_WORDS = 5


def _heading_for(line: str) -> str | None:
    cleaned = re.sub(r"^[^\w]+|[^\w]+$", "", line).strip().lower()
    if not cleaned or len(cleaned.split()) > _MAX_HEADING_WORDS:
        return None
    if cleaned.endswith((".", ",", ";")):
        return None
    for section, heading in _ALL:
        if cleaned == heading or cleaned.replace(":", "").strip() == heading:
            return section
    return None


def segment(text: str) -> dict[str, str]:
    """Split resume text into named sections.

    Everything before the first recognised heading becomes ``header`` — that is
    where the name and contact details live, which is exactly what M4 needs to
    redact most aggressively.
    """
    if not text.strip():
        return {}

    sections: dict[str, list[str]] = {"header": []}
    current = "header"

    for line in text.split("\n"):
        section = _heading_for(line)
        if section is not None:
            current = section
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)

    return {
        name: "\n".join(lines).strip()
        for name, lines in sections.items()
        if "\n".join(lines).strip()
    }


def detect_language(text: str) -> str | None:
    """Best-effort language tag. Returns None rather than guessing on short text."""
    sample = text.strip()
    if len(sample) < 60:
        return None
    try:
        from langdetect import DetectorFactory, detect

        # Without a fixed seed langdetect is non-deterministic, which would make
        # the same resume produce different rows on different runs.
        DetectorFactory.seed = 0
        return str(detect(sample[:4000]))
    except Exception:
        return None

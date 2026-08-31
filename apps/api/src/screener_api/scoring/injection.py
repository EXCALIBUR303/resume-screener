"""Prompt-injection and keyword-stuffing detection.

Two distinct attacks, and I conflated them.

**Instruction injection** tries to make the model obey text in the document.
ADR-0003 showed `qwen3:8b` falling for it completely. Evidence verification
defeats it, because an instruction produces no verifiable quote.

**Keyword stuffing** needs no model at all. Writing "Kubernetes" into a resume
makes a keyword extractor find Kubernetes. The blueprint claimed the
deterministic half was "mathematically immune to injection"; that was **wrong**,
and measured wrong: the injected resume scored 1.00 on skills against the honest
resume's 0.67, purely because the injection sentence named the missing skill.

The fix is to excise detected spans *before* any scoring reads the text, so a
flagged region contributes neither instructions to the model nor keywords to the
arithmetic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger()

# Imperatives addressed at an assistant. Deliberately narrow: a resume that
# happens to say "ignore" in prose must not be flagged.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b"
            r"(?:previous|prior|above|earlier|all)\b[^.\n]{0,30}\b"
            r"(?:instruction|prompt|rule|direction|context)",
            re.I,
        ),
    ),
    (
        "role_reassignment",
        re.compile(
            r"\byou\s+are\s+(?:now\s+)?(?:a|an|the)\b|\bact\s+as\s+(?:a|an|the)\b|"
            r"\bnew\s+(?:system\s+)?(?:prompt|instruction)\b",
            re.I,
        ),
    ),
    (
        "score_demand",
        re.compile(
            r"\b(?:rate|score|rank|grade)\b[^.\n]{0,30}\b(?:10/10|100%|perfect|highest|"
            r"maximum|top)\b|\b(?:perfect|ideal|flawless)\s+(?:match|candidate|fit)\b",
            re.I,
        ),
    ),
    (
        "hiring_demand",
        re.compile(
            r"\b(?:recommend|hire|advance|shortlist|select)\b[^.\n]{0,30}\b"
            r"(?:immediately|without|regardless|automatically)\b",
            re.I,
        ),
    ),
    (
        "concealment",
        re.compile(
            r"\bdo\s+not\s+(?:mention|reveal|disclose|report|output)\b|"
            r"\bwithout\s+(?:mentioning|revealing|telling)\b",
            re.I,
        ),
    ),
    # Closing the fence the document is wrapped in, to escape into the prompt.
    # The per-request nonce makes this useless against the real gateway, but a
    # document attempting it is unambiguously hostile and must be flagged.
    (
        "fence_escape",
        re.compile(
            r"</\s*untrusted_document"
            r"|<\s*/?\s*(?:system|user|assistant|instruction)\s*>"
            r"|\[/?\s*INST\s*\]|<\|(?:im_start|im_end|endoftext)\|>",
            re.I,
        ),
    ),
    (
        "system_impersonation",
        re.compile(
            r"\b(?:system|assistant|developer)\s*[:>]\s|</?\s*(?:system|instruction|prompt)\s*>",
            re.I,
        ),
    ),
)


@dataclass
class InjectionFinding:
    kind: str
    start: int
    end: int
    excerpt: str


@dataclass
class InjectionReport:
    findings: list[InjectionFinding] = field(default_factory=list)
    sanitised_text: str = ""
    removed_chars: int = 0

    @property
    def suspected(self) -> bool:
        return bool(self.findings)

    @property
    def kinds(self) -> list[str]:
        return sorted({f.kind for f in self.findings})


# A flagged sentence is removed along with the rest of its line: an injection is
# rarely alone on a line, and the surrounding clause is not trustworthy either.
def detect(text: str) -> InjectionReport:
    findings: list[InjectionFinding] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            findings.append(
                InjectionFinding(
                    kind=kind, start=match.start(), end=match.end(), excerpt=match.group(0)[:120]
                )
            )

    if not findings:
        return InjectionReport(findings=[], sanitised_text=text, removed_chars=0)

    # Expand each finding to its whole line, then drop those lines.
    doomed: set[int] = set()
    line_bounds: list[tuple[int, int]] = []
    cursor = 0
    for line in text.split("\n"):
        line_bounds.append((cursor, cursor + len(line)))
        cursor += len(line) + 1

    raw_lines = text.split("\n")
    for index, (start, end) in enumerate(line_bounds):
        if any(f.start < end and f.end > start for f in findings):
            doomed.add(index)

    # Extend FORWARD only, to the end of the paragraph.
    #
    # The payload line ("...including Kubernetes and PostgreSQL at massive
    # scale") triggers no pattern of its own and survived line-granular removal,
    # handing the scorer exactly the skills the attacker wanted. It follows the
    # trigger, so forward extension catches it.
    #
    # Extending BACKWARD was the first attempt and it over-removed: an injection
    # appended directly beneath a genuine line took that line with it. A
    # continuation follows its trigger; what precedes one is usually the real
    # resume, and deleting it punishes the candidate for the attacker's
    # formatting.
    for index in sorted(doomed):
        cursor = index + 1
        while cursor < len(raw_lines) and raw_lines[cursor].strip():
            doomed.add(cursor)
            cursor += 1

    kept = [line for i, line in enumerate(text.split("\n")) if i not in doomed]
    sanitised = "\n".join(kept)

    log.warning(
        "scoring.injection_detected",
        kinds=sorted({f.kind for f in findings}),
        lines_removed=len(doomed),
        chars_removed=len(text) - len(sanitised),
    )
    return InjectionReport(
        findings=findings,
        sanitised_text=sanitised,
        removed_chars=len(text) - len(sanitised),
    )


def invisible_text_ratio(rendered: str, extracted: str) -> float:
    """Fraction of extracted text absent from what a human would see.

    White-on-white text and zero-size fonts are the classic delivery vehicle.
    Populated in M6 only when a renderer is available; 0.0 means "not measured",
    never "verified clean".
    """
    if not extracted:
        return 0.0
    visible = set(rendered.split())
    hidden = [w for w in extracted.split() if w not in visible]
    return len(hidden) / max(1, len(extracted.split()))

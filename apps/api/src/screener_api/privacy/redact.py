"""PII redaction and pseudonymisation.

Four layers, because no single one is reliable enough to be a privacy control:

1. **Deterministic patterns** — emails, phones, IDs. Near-perfect recall, no model.
2. **Structural** — the header block (everything above the first section heading)
   is where names and contact details live. It is redacted aggressively by
   position, not by recognition, which is what stops a name the NER misses.
3. **NER** — names, locations and organisations in prose, via Presidio + spaCy.
4. **Protected attributes** — gender, marital status, religion, nationality,
   disability, age proxies. Removed so the model cannot reason about them at all.

Output is *pseudonymised*, not deleted: each entity becomes a stable token
(``PERSON_1``, ``EMAIL_2``). The recruiter's UI can re-hydrate those tokens from
an encrypted map held in the API process, so the model reasons about
``PERSON_1`` while a human sees a person. Privacy and usability, not a trade.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import structlog

from screener_api.privacy.recognizers import (
    EDUCATION_CONTEXT,
    EDUCATION_WINDOW,
    NEVER_REDACT,
    PATTERNS,
    PHONE_MIN_DIGITS,
    PROTECTED_PATTERNS,
    YEAR,
)

log = structlog.get_logger()

# Entities NER contributes. Presidio names them; we map to our own vocabulary.
_NER_ENTITIES = ("PERSON", "LOCATION", "NRP", "ORGANIZATION")
_NER_MAP = {"NRP": "NATIONALITY", "ORGANIZATION": "ORG", "LOCATION": "LOCATION", "PERSON": "PERSON"}

TOKEN_RE = re.compile(r"\b([A-Z_]+)_(\d+)\b")


@dataclass
class Span:
    start: int
    end: int
    entity: str
    text: str
    source: str  # pattern | structural | ner | protected | propagated
    # When set, this span shares a token with another value. A first name and
    # the full name it came from are ONE person: emitting PERSON_1, PERSON_2 and
    # PERSON_3 for "Priya Ramanathan", "Priya" and "Ramanathan" would tell the
    # model there are three candidates in one resume.
    canonical: str | None = None


@dataclass
class RedactionResult:
    text: str
    token_map: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def entity_count(self) -> int:
        return sum(self.counts.values())


@lru_cache(maxsize=1)
def _analyzer() -> Any:
    """Presidio is expensive to construct; build it once per process."""
    import warnings

    warnings.filterwarnings("ignore", category=UserWarning)
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
        }
    )
    return AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["en"])


def _pattern_spans(text: str) -> list[Span]:
    spans: list[Span] = []
    for entity, regex in PATTERNS:
        for m in regex.finditer(text):
            value = m.group(0)
            if entity == "PHONE" and not _is_phone_like(value):
                continue
            spans.append(Span(m.start(), m.end(), entity, value, "pattern"))
    return spans


def _is_phone_like(value: str) -> bool:
    """Reject date ranges and short numeric runs.

    "(2021-2026)" satisfied the shape of a phone number and was redacted as one,
    which destroyed the employment dates the experience score is computed from.
    """
    digits = sum(c.isdigit() for c in value)
    if value.lstrip().startswith("+"):
        return digits >= 7
    return digits >= PHONE_MIN_DIGITS


def _protected_spans(text: str, *, redact_grad_years: bool) -> list[Span]:
    spans: list[Span] = []
    for entity, regex in PROTECTED_PATTERNS:
        for m in regex.finditer(text):
            spans.append(Span(m.start(), m.end(), entity, m.group(0), "protected"))
    if redact_grad_years:
        for m in YEAR.regex.finditer(text):
            # A year counts as a graduation year when an education keyword sits
            # within a window on EITHER side: "B.Tech, Example Institute, 2019"
            # is at least as common as "2019 B.Tech".
            lo = max(0, m.start() - EDUCATION_WINDOW)
            hi = min(len(text), m.end() + EDUCATION_WINDOW)
            window = text[lo:hi]
            if EDUCATION_CONTEXT.search(window):
                spans.append(Span(m.start(), m.end(), YEAR.entity, m.group(0), "protected"))
    return spans


def _ner_spans(text: str) -> list[Span]:
    try:
        results = _analyzer().analyze(text=text, language="en", entities=list(_NER_ENTITIES))
    except Exception as exc:
        # NER is one layer of four. If the model fails to load, the deterministic
        # layers still run — degraded, and loudly, but never silently open.
        log.error("redaction.ner_unavailable", error=type(exc).__name__)
        return []

    spans: list[Span] = []
    for r in results:
        if r.score < 0.4:
            continue
        entity = _NER_MAP.get(r.entity_type, r.entity_type)
        # Split first, filter second. A name does not span a newline, and NER
        # returned both "Priya Ramanathan\npriya@example.com" (one PERSON) and
        # "SKILLS\nPython, PostgreSQL" (one ORG). Checking the allowlist against
        # the whole multi-line span let the technology half through unprotected,
        # because the heading made the combined string fail the check.
        for piece in _split_at_newlines(r.start, r.end, entity, text):
            # A technology is not an employer, even when NER says so. Checked
            # token-by-token: "Redis, Docker" arrives as ONE organisation span,
            # and a whole-string lookup destroyed two skills at once.
            if _is_all_known_technology(piece.text):
                continue
            spans.append(piece)
    return spans


def _split_at_newlines(start: int, end: int, entity: str, text: str) -> list[Span]:
    out: list[Span] = []
    cursor = start
    for line in text[start:end].split("\n"):
        trimmed = _trim(Span(cursor, cursor + len(line), entity, line, "ner"), text)
        if trimmed is not None:
            out.append(trimmed)
        cursor += len(line) + 1
    return out


def _structural_spans(text: str, header: str | None) -> list[Span]:
    """Redact every capitalised name-like line in the header block.

    Names are the entity NER misses most often, and in a resume they are almost
    always in the first few lines. Position is more dependable than recognition
    here, so this layer does not ask a model's opinion.
    """
    if not header:
        return []
    index = text.find(header[:200]) if len(header) >= 20 else text.find(header)
    if index < 0:
        index = 0

    spans: list[Span] = []
    offset = index
    for line in header.split("\n"):
        stripped = line.strip()
        if stripped and _looks_like_a_name(stripped):
            start = text.find(stripped, offset)
            if start >= 0:
                spans.append(Span(start, start + len(stripped), "PERSON", stripped, "structural"))
        offset += len(line) + 1
    return spans


def _looks_like_a_name(line: str) -> bool:
    """1-4 words, mostly capitalised, no digits, not a known heading."""
    words = line.replace(",", " ").split()
    if not 1 <= len(words) <= 4 or any(c.isdigit() for c in line):
        return False
    if "@" in line or "|" in line or ":" in line:
        return False
    alpha = [w for w in words if w.replace("-", "").replace("'", "").isalpha()]
    if len(alpha) != len(words):
        return False
    capitalised = sum(1 for w in words if w[:1].isupper())
    return capitalised >= max(1, len(words) - 1)


def _trim(span: Span, text: str) -> Span | None:
    """Shrink a span to its non-whitespace content.

    NER happily returns a PERSON span that runs past the newline into the next
    line, and replacing it wholesale glued the following token onto the name
    ("PERSON_1EMAIL_1"), destroying the line structure section detection needs.
    """
    start, end = span.start, span.end
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if end <= start:
        return None
    return Span(start, end, span.entity, text[start:end], span.source)


_SPLIT_TOKENS = re.compile(r"[\s,;/|&]+|\band\b", re.I)


# Words that glue technology names together in prose. NER hands back
# "Python on PostgreSQL" as a single organisation; without ignoring the
# connector the whole span failed the allowlist and two skills were destroyed.
_CONNECTORS = frozenset(
    {
        "on",
        "in",
        "with",
        "using",
        "and",
        "or",
        "for",
        "the",
        "a",
        "an",
        "via",
        "over",
        "plus",
        "to",
        "at",
        "of",
        "apis",
        "api",
        "stack",
    }
)


def _is_all_known_technology(value: str) -> bool:
    """True when every meaningful token in the span is a known technology."""
    parts = [p.strip(" .,;:()[]") for p in _SPLIT_TOKENS.split(value)]
    meaningful = [p for p in parts if p and p.lower() not in _CONNECTORS]
    return bool(meaningful) and all(p.lower() in NEVER_REDACT for p in meaningful)


def _name_parts(value: str) -> list[str]:
    """Individual name tokens worth redacting on their own.

    A resume routinely refers to the candidate by first name after introducing
    the full name ("Priya designed payment services..."). Propagating only exact
    full-string matches left that standalone first name in the text sent to the
    model — a real leak the fixture corpus missed because its body always used
    the full name.
    """
    parts = []
    for raw in re.split(r"[\s,]+", value):
        token = raw.strip("-'.")
        if len(token) >= 3 and token[:1].isupper() and token.isalpha():
            parts.append(token)
    return parts if len(parts) > 1 else []


def _propagate_known_values(text: str, spans: list[Span]) -> list[Span]:
    """Redact every occurrence of a value once any layer has identified it.

    A name found structurally in the header is a *known* PII value. Leaving a
    later mention of it in the body because NER happened to miss that sentence
    is a leak, and it is the leak most likely when the NER model is weak or
    unavailable. Propagation makes the four layers reinforce each other instead
    of each covering only what it personally spotted.
    """
    propagated: list[Span] = []
    seen = {(s.start, s.end) for s in spans}

    for span in spans:
        if span.entity not in ("PERSON", "ORG", "LOCATION"):
            continue
        value = span.text.strip()
        if len(value) < 4:
            continue

        # A resume introduces the full name once and then uses the first name
        # ("Priya designed payment services..."). Propagating only exact
        # full-string matches left that standalone first name in the text sent
        # to the model.
        candidates = [value, *(_name_parts(value) if span.entity == "PERSON" else [])]

        for candidate in candidates:
            if len(candidate) < 3:
                continue
            pattern = (
                rf"\b{re.escape(candidate)}\b"
                if candidate[:1].isalnum() and candidate[-1:].isalnum()
                else re.escape(candidate)
            )
            for m in re.finditer(pattern, text):
                key = (m.start(), m.end())
                if key in seen:
                    continue
                seen.add(key)
                propagated.append(
                    Span(
                        m.start(),
                        m.end(),
                        span.entity,
                        m.group(0),
                        "propagated",
                        canonical=value,
                    )
                )
    return propagated


def _merge(spans: list[Span]) -> list[Span]:
    """Resolve overlaps: longest span wins, ties broken by layer priority."""
    priority = {"pattern": 0, "structural": 1, "protected": 2, "ner": 3, "propagated": 4}
    ordered = sorted(spans, key=lambda s: (s.start, -(s.end - s.start), priority.get(s.source, 9)))
    merged: list[Span] = []
    for span in ordered:
        if merged and span.start < merged[-1].end:
            continue  # fully or partially covered by a longer earlier span
        merged.append(span)
    return merged


def redact(
    text: str,
    *,
    header: str | None = None,
    redact_grad_years: bool = True,
    use_ner: bool = True,
) -> RedactionResult:
    """Return pseudonymised text plus the token -> original map.

    The map is the only thing that can reverse this, and it is stored encrypted
    and separately from the redacted text (see M4 schema).
    """
    if not text.strip():
        return RedactionResult(text=text)

    spans = _pattern_spans(text)
    spans += _structural_spans(text, header)
    spans += _protected_spans(text, redact_grad_years=redact_grad_years)
    if use_ner:
        spans += _ner_spans(text)

    trimmed = [t for t in (_trim(s, text) for s in spans) if t is not None]
    trimmed += _propagate_known_values(text, trimmed)
    merged = _merge(trimmed)

    token_map: dict[str, str] = {}
    reverse: dict[str, str] = {}
    counters: dict[str, int] = {}
    counts: dict[str, int] = {}

    out: list[str] = []
    cursor = 0
    for span in merged:
        original = span.text.strip()
        if not original:
            continue
        # Group by the canonical value when one is set, so every mention of a
        # person collapses to a single token.
        grouping = (span.canonical or original).lower()
        key = f"{span.entity}\x1f{grouping}"
        token = reverse.get(key)
        if token is None:
            counters[span.entity] = counters.get(span.entity, 0) + 1
            token = f"{span.entity}_{counters[span.entity]}"
            reverse[key] = token
            token_map[token] = original
        counts[span.entity] = counts.get(span.entity, 0) + 1

        out.append(text[cursor : span.start])
        out.append(token)
        cursor = span.end
    out.append(text[cursor:])

    return RedactionResult(text="".join(out), token_map=token_map, counts=counts)


def rehydrate(text: str, token_map: dict[str, str]) -> str:
    """Put the real values back, for display to an authorised human only."""
    return TOKEN_RE.sub(lambda m: token_map.get(m.group(0), m.group(0)), text)

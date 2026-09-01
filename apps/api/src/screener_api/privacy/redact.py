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

import bisect
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import structlog

from screener_api.privacy.recognizers import (
    AMBIGUOUS_PROTECTED_PATTERNS,
    DELETE_NOT_TOKENISE,
    EDUCATION_CONTEXT,
    EDUCATION_WINDOW,
    NEVER_REDACT,
    PATTERNS,
    PHONE_MIN_DIGITS,
    POSTAL_CONTEXT,
    POSTAL_US,
    POSTAL_WINDOW,
    PROTECTED_CONTEXT,
    PROTECTED_PATTERNS,
    PROTECTED_WINDOW,
    YEAR,
    is_degree_phrase,
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


def _matched_span(m: re.Match[str]) -> tuple[int, int]:
    """The characters a pattern wants removed.

    Usually the whole match, but a pattern may mark a narrower `redact` group
    when it needs surrounding context to identify the target without also
    destroying it — "Career break 2022-2023 (parental leave)" has to see the
    phrase and the dates to know what the parenthetical is, and has to leave
    them behind because the experience arithmetic reads them.
    """
    if "redact" in (m.re.groupindex or {}) and m.group("redact") is not None:
        return m.span("redact")
    return m.span()


def _preceded_by(text: str, start: int, cue: re.Pattern[str], window: int) -> bool:
    """True when `cue` appears in the `window` characters just before `start`.

    Cues that anchor with `$` therefore mean "immediately before the match",
    which is how a US state abbreviation identifies the digits after it as a ZIP.
    """
    return bool(cue.search(text[max(0, start - window) : start]))


def _near(text: str, start: int, end: int, cue: re.Pattern[str], window: int) -> bool:
    """True when `cue` appears within `window` characters on either side."""
    return bool(cue.search(text[max(0, start - window) : min(len(text), end + window)]))


def _pattern_spans(text: str) -> list[Span]:
    spans: list[Span] = []
    for entity, regex in PATTERNS:
        for m in regex.finditer(text):
            start, end = _matched_span(m)
            value = text[start:end]
            if entity == "PHONE" and not _is_phone_like(value):
                continue
            spans.append(Span(start, end, entity, value, "pattern"))

    # A five-digit number is only a postal code when an address says so. Bare,
    # this pattern redacted "50000 concurrent connections" and "12000 ms" —
    # quantified impact is the most valuable thing on a resume, and destroying
    # it is the same class of failure as ADR-0009, not a safe over-redaction.
    for m in POSTAL_US.regex.finditer(text):
        if _preceded_by(text, m.start(), POSTAL_CONTEXT, POSTAL_WINDOW):
            spans.append(Span(m.start(), m.end(), POSTAL_US.entity, m.group(0), "pattern"))
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


# Where the value after a "Label: value" field stops. Ends at a line break, an
# explicit separator, or the next label — so "Nationality: Indian | Languages:
# English" gives up the nationality without taking the languages with it.
_FIELD_VALUE_END = re.compile(r"\n|[|;]|(?=\b[\w ]{1,20}:)")


def _extend_over_field_value(text: str, span: Span) -> Span:
    """Cover the value of a `Label: value` field, not just the label.

    Several protected terms ARE the label — "Nationality", "Citizenship",
    "Visa status". Matching them alone produced `NATIONALITY_1: Indian`: the
    word naming the attribute was removed and the attribute itself was left in
    the text handed to the model. The same inversion as the degree/institution
    one, and just as backwards (ADR-0017).
    """
    rest = text[span.end :]
    separator = re.match(r"\s*[:\-]\s*", rest)
    if separator is None:
        return span
    value_start = span.end + separator.end()
    stop = _FIELD_VALUE_END.search(text, value_start)
    value_end = stop.start() if stop else len(text)
    if value_end <= value_start:
        return span
    return Span(span.start, value_end, span.entity, text[span.start : value_end], span.source)


def _protected_spans(text: str, *, redact_grad_years: bool) -> list[Span]:
    spans: list[Span] = []
    for entity, regex in PROTECTED_PATTERNS:
        for m in regex.finditer(text):
            start, end = _matched_span(m)
            span = Span(start, end, entity, text[start:end], "protected")
            spans.append(_extend_over_field_value(text, span))
    # Terms that are a protected attribute on a form and ordinary engineering
    # vocabulary in prose. Only redacted when a demographic cue sits nearby, so
    # "Marital status: single" goes and "single point of failure" stays.
    for entity, regex in AMBIGUOUS_PROTECTED_PATTERNS:
        for m in regex.finditer(text):
            if _near(text, m.start(), m.end(), PROTECTED_CONTEXT, PROTECTED_WINDOW):
                spans.append(Span(m.start(), m.end(), entity, m.group(0), "protected"))

    if redact_grad_years:
        for m in YEAR.regex.finditer(text):
            # A year counts as a graduation year when an education keyword sits
            # within a window on EITHER side: "B.Tech, Example Institute, 2019"
            # is at least as common as "2019 B.Tech".
            if _near(text, m.start(), m.end(), EDUCATION_CONTEXT, EDUCATION_WINDOW):
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
            # Neither is a degree. NER returns "B.Tech Computer Science" as an
            # ORGANIZATION, so the qualification was redacted while the
            # institution sitting beside it on the same line survived — exactly
            # backwards, since the degree is the signal and the institution is
            # the proxy for background (ADR-0017).
            if is_degree_phrase(piece.text):
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
    # Trailing periods are allowed so an honorific or an initial does not
    # disqualify the whole line. "Ms. Alex Placeholder" failed this check and
    # was therefore never redacted by the structural layer — the layer that
    # exists precisely to catch the names NER misses.
    alpha = [w for w in words if w.replace("-", "").replace("'", "").rstrip(".").isalpha()]
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


LAYER_PRIORITY = {"pattern": 0, "structural": 1, "protected": 2, "ner": 3, "propagated": 4}


def _merge(spans: list[Span]) -> list[Span]:
    """Resolve overlaps: the most reliable layer wins, then the longest span.

    The previous version claimed "longest span wins" and did not do it. It
    sorted by start position and dropped anything overlapping the span before
    it, so a *shorter, earlier-starting* span suppressed a *longer, later* one.
    Concretely: NER returned a five-character PERSON span `"| +91"` at offset 34
    and the phone pattern matched `"+91 90000 00000"` at offset 36. The
    fragment won, the phone match was discarded, and the digits were then
    re-matched by weaker patterns and emitted as `PERSON_2 POSTAL_US_1
    POSTAL_US_2`.

    Whether NER produced that fragment depended on the candidate's *name*, so
    two identical resumes redacted to different text depending on who they
    belonged to. That is the bug ADR-0017 is about; ordering by layer first is
    the fix, and it also matches the four-layer design's own premise that the
    deterministic patterns carry the recall and NER only fills gaps.
    """
    ordered = sorted(
        spans, key=lambda s: (LAYER_PRIORITY.get(s.source, 9), -(s.end - s.start), s.start)
    )
    # Kept spans are held sorted by start so the overlap check is a binary
    # search against two neighbours rather than a scan. The input is attacker
    # controlled — a crafted upload can produce thousands of spans — and a
    # quadratic check here would be a cheap way to stall the parse worker.
    starts: list[int] = []
    accepted: list[Span] = []
    for span in ordered:
        i = bisect.bisect_left(starts, span.start)
        before_overlaps = i > 0 and accepted[i - 1].end > span.start
        after_overlaps = i < len(accepted) and accepted[i].start < span.end
        if before_overlaps or after_overlaps:
            continue  # a more reliable layer already claimed these characters
        starts.insert(i, span.start)
        accepted.insert(i, span)
    return accepted


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
        # Protected attributes leave nothing behind, not even a token. See
        # DELETE_NOT_TOKENISE for why. The sentinel is removed below along with
        # whatever punctuation was holding it in the line.
        out.append(_DELETED if span.entity in DELETE_NOT_TOKENISE else token)
        cursor = span.end
    out.append(text[cursor:])

    return RedactionResult(text=_clean_deletions("".join(out)), token_map=token_map, counts=counts)


# Marks where a protected attribute was, so the punctuation around it can be
# cleaned up before it is dropped. \x00 cannot appear in extracted text.
_DELETED = "\x00"

# A deletion takes its list separator with it. Without this, removing the
# pronoun line from "EMAIL_1 | PHONE_1 | (she/her)" left "EMAIL_1 | PHONE_1 |",
# and a trailing pipe is still a disclosure — it says a field was there.
_SEPARATOR_BEFORE = re.compile(r"[ \t]*[|,;·][ \t]*" + _DELETED)
_SEPARATOR_AFTER = re.compile(_DELETED + r"[ \t]*[|,;·][ \t]*")
_ENCLOSING_PARENS = re.compile(r"\([ \t]*" + _DELETED + r"[ \t]*\)")


def _clean_deletions(text: str) -> str:
    """Remove deletion sentinels along with the punctuation that framed them.

    A line left holding only whitespace is dropped entirely. Half of the
    residual signal this function exists to remove was structural rather than
    lexical: not what the line said, but that there was a line.
    """
    if _DELETED not in text:
        return text

    text = _ENCLOSING_PARENS.sub(_DELETED, text)
    text = _SEPARATOR_BEFORE.sub(_DELETED, text)
    text = _SEPARATOR_AFTER.sub(_DELETED, text)

    kept: list[str] = []
    for line in text.split("\n"):
        if _DELETED not in line:
            kept.append(line)
            continue
        if not line.replace(_DELETED, "").strip():
            continue  # the line existed only to carry the attribute
        # Tidy the hole left behind. A doubled space or a space before a full
        # stop is not a privacy problem, but it is a difference between two
        # resumes that are supposed to be indistinguishable.
        stripped = _ORPHANED_SPACE.sub(" ", line.replace(_DELETED, ""))
        kept.append(_SPACE_BEFORE_PUNCTUATION.sub(r"\1", stripped).rstrip())
    return "\n".join(kept)


_ORPHANED_SPACE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"[ \t]+([.,;:)])")


def rehydrate(text: str, token_map: dict[str, str]) -> str:
    """Put the real values back, for display to an authorised human only."""
    return TOKEN_RE.sub(lambda m: token_map.get(m.group(0), m.group(0)), text)

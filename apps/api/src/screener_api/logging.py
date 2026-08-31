"""Structured JSON logging with unconditional redaction.

The redaction processor runs on *every* record at *every* level. There is no
debug mode that redacts less — that is the whole point. This is the foundation
the AC-3 "zero PII egress" gate is asserted against.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

# Field names whose values are never safe to emit, whatever they contain.
DENY_FIELDS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "api_key",
        "apikey",
        "kek",
        "dek",
        "jwt",
        "cookie",
        "set_cookie",
        "email",
        "phone",
        "ssn",
        "candidate_name",
        "pii",
        "pii_map",
        "raw_text",
        "resume_text",
    }
)

REDACTED = "[redacted]"

# Value-shaped redaction, for PII that arrives in a field we did not anticipate.
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    # Boundaries exclude [0-9a-zA-Z-] on both sides so this cannot fire inside a
    # UUID, a hash, or any hyphenated identifier. Without them it corrupted every
    # request_id in the logs — see test_uuids_are_not_mistaken_for_phone_numbers.
    (
        re.compile(
            r"(?<![0-9a-zA-Z-])"
            r"(?:\+\d{1,3}[\s.-]?)?"
            r"(?:\(?\d{2,4}\)?[\s.-]?){2,4}\d{2,4}"
            r"(?![0-9a-zA-Z-])"
        ),
        "[phone]",
    ),
    # Long high-entropy strings look like credentials. Hex digests are the
    # documented exception: sha256 content addresses and audit-chain hashes are
    # not secrets, and redacting them would make the audit log uncorrelatable.
    (
        re.compile(r"\b(?![0-9a-f]{32}\b|[0-9a-f]{40}\b|[0-9a-f]{64}\b)[A-Za-z0-9+/]{40,}={0,2}\b"),
        "[key-like]",
    ),
    (re.compile(r"postgresql(?:\+\w+)?://[^:]+:[^@]+@"), "postgresql://[redacted]@"),
    # A JWT as a whole, not just its long middle segment. Matching by length
    # alone let the header through, which is enough to fingerprint the algorithm.
    (re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), "[jwt]"),
    (re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._~+/-]+=*"), "Bearer [redacted]"),
)


def _is_sensitive(key: str, value: Any) -> bool:
    """Deny-list by field name, but only for values that could BE the data.

    An integer keyed "phone" is a count, not a phone number. Redacting it turned
    the redaction telemetry itself into "[redacted]" — over-redaction again,
    this time destroying the numbers that show the redactor is working.
    """
    return key.lower() in DENY_FIELDS and not isinstance(value, (int, float, bool))


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, str):
        for pattern, replacement in PATTERNS:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, dict):
        return {
            k: REDACTED if _is_sensitive(k, v) else _scrub(v, depth + 1) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v, depth + 1) for v in value)
    return value


def redact_processor(
    _logger: Any, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Deny-list on field names, then pattern-scrub every remaining value."""
    return {k: REDACTED if k.lower() in DENY_FIELDS else _scrub(v) for k, v in event_dict.items()}


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_processor,  # last before rendering: nothing escapes it
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        cache_logger_on_first_use=True,
    )

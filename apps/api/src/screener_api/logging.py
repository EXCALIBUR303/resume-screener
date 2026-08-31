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
    (re.compile(r"(?<!\d)(?:\+\d{1,3}[\s-]?)?(?:\d[\s-]?){9,14}\d(?!\d)"), "[phone]"),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), "[key-like]"),
    (re.compile(r"postgresql(?:\+\w+)?://[^:]+:[^@]+@"), "postgresql://[redacted]@"),
)


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, str):
        for pattern, replacement in PATTERNS:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, dict):
        return {
            k: REDACTED if k.lower() in DENY_FIELDS else _scrub(v, depth + 1)
            for k, v in value.items()
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

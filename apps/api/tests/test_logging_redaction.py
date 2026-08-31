"""The foundation of AC-3: nothing sensitive survives a log record."""

from __future__ import annotations

import pytest

from screener_api.logging import REDACTED, redact_processor


def scrub(**event: object) -> dict[str, object]:
    return redact_processor(None, "", event)


def test_denied_field_names_are_removed() -> None:
    out = scrub(password="hunter2", jwt="ey.abc", email="a@b.com")
    assert out == {"password": REDACTED, "jwt": REDACTED, "email": REDACTED}


def test_denied_names_are_case_insensitive() -> None:
    assert scrub(Authorization="Bearer x")["Authorization"] == REDACTED


@pytest.mark.parametrize(
    "text",
    [
        "contact priya.r@gmail.com today",
        "call +91 98765 44321 now",
        "dsn postgresql+psycopg://screener:s3cret@db:5432/screener",
    ],
)
def test_pii_shaped_values_are_scrubbed_in_unexpected_fields(text: str) -> None:
    """PII that lands in a field we did not anticipate is still caught."""
    out = str(scrub(note=text)["note"])
    for leaked in ("priya.r@gmail.com", "98765 44321", "s3cret"):
        assert leaked not in out


def test_nested_structures_are_scrubbed() -> None:
    out = scrub(payload={"user": {"email": "x@y.com", "password": "p"}, "tags": ["a@b.com"]})
    rendered = str(out)
    assert "x@y.com" not in rendered
    assert "a@b.com" not in rendered
    assert "[email]" in rendered or REDACTED in rendered


def test_ordinary_values_survive() -> None:
    out = scrub(event="request.completed", status=200, duration_ms=12.5)
    assert out["event"] == "request.completed"
    assert out["status"] == 200


def test_recursion_is_bounded() -> None:
    """A deeply nested payload must not blow the stack."""
    deep: dict[str, object] = {"k": "a@b.com"}
    for _ in range(50):
        deep = {"k": deep}
    scrub(payload=deep)  # must not raise


# ---- Regressions: over-redaction is a bug too ----------------------------------
# The redactor corrupted every request_id in the logs before these existed
# ("3ed85c6e-7fcd-[phone]a8c49bd0"). A redactor that mangles identifiers destroys
# the correlation the logs exist for, so precision is a requirement, not a nicety.


def test_uuids_are_not_mistaken_for_phone_numbers() -> None:
    import uuid as _uuid

    for _ in range(200):
        value = str(_uuid.uuid4())
        assert scrub(request_id=value)["request_id"] == value


def test_iso_timestamps_survive() -> None:
    stamp = "2026-08-31T12:40:08.944259Z"
    assert scrub(timestamp=stamp)["timestamp"] == stamp


def test_sha256_digests_survive() -> None:
    """Content addresses and audit-chain hashes are not secrets. Redacting them
    would make the audit log impossible to correlate."""
    digest = "3f5a" * 16
    assert scrub(hash=digest)["hash"] == digest


def test_durations_and_counts_survive() -> None:
    out = scrub(duration_ms=1234.56, count=987654321, port=5432)
    assert out["duration_ms"] == 1234.56
    assert out["count"] == 987654321


@pytest.mark.parametrize(
    "text",
    [
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
        "Authorization: Bearer abc123def456ghi789",
    ],
)
def test_credentials_in_free_text_are_redacted(text: str) -> None:
    out = str(scrub(note=text)["note"])
    assert "eyJ" not in out
    assert "abc123def456ghi789" not in out


@pytest.mark.parametrize(
    "number", ["+91 98765 44321", "+1 (555) 123-4567", "+44 20 7946 0958", "9876543210"]
)
def test_real_phone_numbers_are_still_redacted(number: str) -> None:
    """Tightening the pattern must not have opened a hole."""
    assert number not in str(scrub(note=f"call {number} now")["note"])


def test_counts_are_not_mistaken_for_the_data_they_count() -> None:
    """Redaction telemetry read `{"PHONE": "[redacted]"}` — the count of phone
    numbers removed was itself redacted. An integer keyed "phone" is a count,
    not a phone number, and destroying it hides whether redaction is working."""
    out = scrub(counts={"PHONE": 2, "EMAIL": 1, "PERSON": 3}, entities=6)
    assert out["counts"] == {"PHONE": 2, "EMAIL": 1, "PERSON": 3}
    assert out["entities"] == 6


def test_string_values_under_denied_names_are_still_redacted() -> None:
    assert scrub(phone="+91 98765 44321")["phone"] == REDACTED
    assert scrub(email="a@b.com")["email"] == REDACTED

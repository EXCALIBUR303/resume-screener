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

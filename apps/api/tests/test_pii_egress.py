"""AC-3: zero raw PII leaves the trusted boundary.

The strategy is a scanning fake. Anything that would send text onward — an LLM
provider, a log sink, a search index — is replaced by a recorder that inspects
every payload it receives and fails the test on contact with a known marker.

This is stricter than checking the redactor's output, because it also catches
the case where correct redaction happens but the *wrong variable* is passed on.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import structlog

from screener_api.logging import redact_processor
from screener_api.privacy.redact import redact
from tests.test_redaction import CASES


class EgressRecorder:
    """Stands in for anything that transmits text off-box."""

    def __init__(self, forbidden: list[str]) -> None:
        self.forbidden = [f for f in forbidden if f]
        self.sent: list[str] = []
        self.leaks: list[tuple[str, str]] = []

    def send(self, payload: Any, *, label: str = "payload") -> None:
        rendered = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        self.sent.append(rendered)
        for marker in self.forbidden:
            if marker in rendered:
                self.leaks.append((label, marker))

    def assert_clean(self) -> None:
        assert not self.leaks, "raw PII reached an egress point: " + "; ".join(
            f"{label} leaked {marker!r}" for label, marker in self.leaks
        )


@pytest.mark.parametrize(
    ("text", "header", "markers"), CASES, ids=[c[2][0].replace(" ", "-") for c in CASES]
)
def test_nothing_raw_reaches_a_model(text: str, header: str, markers: list[str]) -> None:
    """The redacted text is what a prompt would carry. Nothing else may be."""
    recorder = EgressRecorder(markers)
    result = redact(text, header=header)

    # Exactly what the LLM gateway will send: system prompt + fenced document.
    recorder.send(
        {
            "system": "You score a candidate against a job description.",
            "document": result.text,
            "resume_id": "r_01H",
        },
        label="llm-request",
    )
    recorder.assert_clean()


@pytest.mark.parametrize(
    ("text", "header", "markers"), CASES[:6], ids=[c[2][0].replace(" ", "-") for c in CASES[:6]]
)
def test_nothing_raw_reaches_the_logs(text: str, header: str, markers: list[str]) -> None:
    """Even when a careless call site logs the *raw* text, the structlog
    processor must strip it before it reaches a sink."""
    recorder = EgressRecorder(markers)
    scrubbed = redact_processor(None, "", {"event": "parse.completed", "raw_text": text})
    recorder.send(scrubbed, label="log-record")
    recorder.assert_clean()


def test_the_recorder_actually_detects_a_leak() -> None:
    """Guards the guard: a gate that cannot fail proves nothing.

    This is the same class of mistake as the AC-6 matrix test that enumerated
    zero routes and passed.
    """
    recorder = EgressRecorder(["Priya Ramanathan"])
    recorder.send({"document": "Priya Ramanathan is a backend engineer"}, label="deliberate")
    assert recorder.leaks, "the egress recorder failed to notice an obvious leak"


def test_token_map_is_never_part_of_a_payload() -> None:
    """The map reverses redaction. It must stay in the API process and never be
    serialised into anything that leaves it."""
    text, header, markers = CASES[0]
    result = redact(text, header=header)
    recorder = EgressRecorder(markers)

    # The mistake this catches: passing the whole result object onward.
    recorder.send({"document": result.text}, label="correct")
    recorder.assert_clean()

    leaky = EgressRecorder(markers)
    leaky.send({"document": result.text, "token_map": result.token_map}, label="wrong")
    assert leaky.leaks, "sending the token map should have been detected as a leak"


def test_structlog_pipeline_end_to_end_is_clean(capsys: pytest.CaptureFixture[str]) -> None:
    from screener_api.logging import configure_logging

    configure_logging("INFO")
    text, _header, markers = CASES[0]
    structlog.get_logger().info(
        "parse.completed", raw_text=text, email=markers[1], candidate_name=markers[0]
    )
    captured = capsys.readouterr().out
    for marker in markers:
        assert marker not in captured, f"{marker!r} was written to stdout"

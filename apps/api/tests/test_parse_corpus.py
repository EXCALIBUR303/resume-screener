"""AC-1: parse coverage across the 24-file fixture corpus.

The criterion is not "extract text from everything" — some documents genuinely
have none. It is that **nothing is ever silently empty**: a document that yields
no usable text must carry an explicit status a downstream stage can act on.
"""

from __future__ import annotations

import pytest

from screener_api.parse.extract import MIN_USEFUL_CHARS, ParseStatus
from screener_api.parse.pipeline import parse_bytes
from tests.fixtures_resumes import MARKER, build_corpus

CORPUS = build_corpus()
FLAGGED = {ParseStatus.NEEDS_OCR, ParseStatus.NO_TEXT, ParseStatus.UNSUPPORTED}


@pytest.mark.parametrize("fixture", CORPUS, ids=[f.name for f in CORPUS])
def test_every_document_parses_or_is_flagged(fixture) -> None:
    """The AC-1 property, per document."""
    outcome = parse_bytes(fixture.data, mime=fixture.mime, ocr_enabled=False)

    if fixture.expect_text:
        assert outcome.status is ParseStatus.PARSED, (
            f"{fixture.name} ({fixture.note}) produced {outcome.status} with "
            f"{len(outcome.extraction.text)} chars"
        )
        assert len(outcome.extraction.text) >= MIN_USEFUL_CHARS
    else:
        assert outcome.status in FLAGGED, (
            f"{fixture.name} yielded no usable text but was not flagged (status={outcome.status})"
        )


def test_ac1_coverage_threshold() -> None:
    """>=95% of the corpus resolves to a definite outcome. A status of
    PARSED-with-nothing-in-it would fail this, which is the point."""
    definite = 0
    for fixture in CORPUS:
        outcome = parse_bytes(fixture.data, mime=fixture.mime, ocr_enabled=False)
        parsed_well = (
            outcome.status is ParseStatus.PARSED
            and len(outcome.extraction.text) >= MIN_USEFUL_CHARS
        )
        if parsed_well or outcome.status in FLAGGED:
            definite += 1

    coverage = definite / len(CORPUS)
    assert coverage >= 0.95, f"AC-1 coverage {coverage:.0%} (need 95%)"


def test_corpus_is_at_least_twenty_four_documents() -> None:
    assert len(CORPUS) >= 24


def test_corpus_is_entirely_synthetic() -> None:
    """Policy, enforced: no real person's resume, ever — including your own."""
    for fixture in CORPUS:
        if not fixture.expect_text:
            continue
        outcome = parse_bytes(fixture.data, mime=fixture.mime, ocr_enabled=False)
        # Case-insensitive: one fixture lowercases its whole document to test
        # heading detection, which lowercases the marker along with everything else.
        haystack = outcome.extraction.text.lower()
        assert MARKER.lower() in haystack or "fictional" in haystack, (
            f"{fixture.name} carries no synthetic-data marker"
        )


def test_sections_are_found_in_structured_resumes() -> None:
    outcome = parse_bytes(
        next(f for f in CORPUS if f.name == "pdf-standard").data,
        mime="application/pdf",
        ocr_enabled=False,
    )
    assert {"experience", "education", "skills"} <= set(outcome.sections)
    assert "header" in outcome.sections


def test_lowercase_headings_are_still_recognised() -> None:
    outcome = parse_bytes(
        next(f for f in CORPUS if f.name == "pdf-lowercase-headings").data,
        mime="application/pdf",
        ocr_enabled=False,
    )
    assert "experience" in outcome.sections


def test_table_content_is_extracted_from_docx() -> None:
    outcome = parse_bytes(
        next(f for f in CORPUS if f.name == "docx-with-table").data,
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ocr_enabled=False,
    )
    assert "PostgreSQL" in outcome.extraction.text


def test_language_is_detected_for_english_resumes() -> None:
    outcome = parse_bytes(
        next(f for f in CORPUS if f.name == "pdf-standard").data,
        mime="application/pdf",
        ocr_enabled=False,
    )
    assert outcome.language == "en"


def test_language_is_none_rather_than_guessed_on_short_text() -> None:
    outcome = parse_bytes(
        next(f for f in CORPUS if f.name == "pdf-very-short").data,
        mime="application/pdf",
        ocr_enabled=False,
    )
    assert outcome.language is None


def test_image_only_pdf_requests_ocr() -> None:
    """The case OCR exists for: valid PDF, real page, zero text layer."""
    outcome = parse_bytes(
        next(f for f in CORPUS if f.name == "pdf-image-only").data,
        mime="application/pdf",
        ocr_enabled=False,
    )
    assert outcome.status is ParseStatus.NEEDS_OCR


def test_parsing_is_deterministic() -> None:
    """Same bytes, same result — required for the idempotency guarantee."""
    fixture = next(f for f in CORPUS if f.name == "pdf-standard")
    first = parse_bytes(fixture.data, mime=fixture.mime, ocr_enabled=False)
    second = parse_bytes(fixture.data, mime=fixture.mime, ocr_enabled=False)
    assert first.extraction.text == second.extraction.text
    assert first.sections == second.sections


def test_unsupported_mime_is_terminal() -> None:
    from screener_api.queue import TerminalError

    with pytest.raises(TerminalError):
        parse_bytes(b"whatever", mime="text/plain")


# ---- OCR status semantics -----------------------------------------------------


def _extraction(text: str, *, ocr_used: bool, confidence: int | None = None):
    from screener_api.parse.extract import Extraction

    return Extraction(
        text=text,
        extractor="tesseract" if ocr_used else "pypdf",
        page_count=1,
        chars_per_page=float(len(text)),
        ocr_used=ocr_used,
        ocr_confidence=confidence,
    )


def test_successful_ocr_of_short_text_is_not_reported_as_empty() -> None:
    """Regression: OCR recovered 137 legible characters at 95% confidence and the
    status still read `no_text`, which would tell a recruiter a readable scan was
    blank. Short and empty are different states."""
    outcome = _extraction(
        "Zoe Fictional Senior Backend Engineer Python PostgreSQL", ocr_used=True, confidence=95
    )
    assert outcome.status is ParseStatus.LOW_TEXT
    assert outcome.status is not ParseStatus.NO_TEXT


def test_genuinely_empty_ocr_is_no_text() -> None:
    assert _extraction("", ocr_used=True, confidence=0).status is ParseStatus.NO_TEXT
    assert _extraction("  a ", ocr_used=True, confidence=10).status is ParseStatus.NO_TEXT


def test_low_text_keeps_its_content_and_asks_for_review() -> None:
    from screener_api.parse.extract import MIN_USEFUL_CHARS

    text = "Zoe Fictional, Backend Engineer at Invented Systems. Python and PostgreSQL."
    assert len(text) < MIN_USEFUL_CHARS
    assert _extraction(text, ocr_used=True, confidence=90).status is ParseStatus.LOW_TEXT


def test_sparse_text_layer_without_ocr_still_asks_for_ocr() -> None:
    assert _extraction("tiny", ocr_used=False).status is ParseStatus.NEEDS_OCR


def test_worker_parse_declares_no_network() -> None:
    """The single most important line in the compose file.

    worker-parse is the only process that touches attacker-controlled bytes, so
    it is given nothing to exfiltrate with. Verified empirically too: inside the
    container every outbound connect fails and DNS cannot resolve `db`, yet
    Postgres is reachable over a shared unix socket.
    """
    import pathlib

    compose = pathlib.Path(__file__).resolve().parents[3] / "docker-compose.yml"
    text = compose.read_text()
    block = text.split("worker-parse:", 1)[1].split("\nvolumes:", 1)[0]

    for required in (
        "network_mode: none",
        "read_only: true",
        'cap_drop: ["ALL"]',
        "no-new-privileges:true",
    ):
        assert required in block, f"worker-parse lost its hardening: {required!r}"

    # It must not be attached to any bridge network either.
    assert "networks:" not in block

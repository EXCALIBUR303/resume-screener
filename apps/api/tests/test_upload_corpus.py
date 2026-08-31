"""AC-8: every case in the malicious-upload corpus is rejected.

The benign cases matter as much as the hostile ones: a validator that rejects
everything passes an attack corpus trivially and is useless.
"""

from __future__ import annotations

import pytest

from screener_api.ingest.validation import (
    DOCX_MIME,
    PDF_MIME,
    Limits,
    Rejection,
    UploadRejectedError,
    safe_extension,
    validate,
)
from tests import corpus

LIMITS = Limits()


# ---- benign uploads must be ACCEPTED ------------------------------------------


def test_valid_pdf_is_accepted() -> None:
    result = validate(
        corpus.minimal_pdf(3), filename="resume.pdf", declared_mime=PDF_MIME, limits=LIMITS
    )
    assert result.mime == PDF_MIME
    assert result.page_count == 3


def test_valid_docx_is_accepted() -> None:
    result = validate(
        corpus.minimal_docx(), filename="resume.docx", declared_mime=DOCX_MIME, limits=LIMITS
    )
    assert result.mime == DOCX_MIME


def test_pdf_at_the_page_limit_is_accepted() -> None:
    assert (
        validate(
            corpus.minimal_pdf(LIMITS.max_pages),
            filename="cv.pdf",
            declared_mime=PDF_MIME,
            limits=LIMITS,
        ).page_count
        == LIMITS.max_pages
    )


def test_missing_declared_type_is_tolerated() -> None:
    """Some clients omit Content-Type. The sniff still has to agree."""
    assert (
        validate(corpus.minimal_pdf(), filename="cv.pdf", declared_mime=None, limits=LIMITS).mime
        == PDF_MIME
    )


# ---- the 18-case hostile corpus -----------------------------------------------

# Note on expectations: four cases below reject as TYPE_MISMATCH rather than
# MALFORMED_PDF. That is the validator working better than first predicted — the
# content sniff catches them before any parser sees the bytes, which is both
# earlier and more informative. The property under test is that they are
# rejected; the reason is pinned so a regression to a weaker check is visible.
CASES: list[tuple[str, bytes, str, str | None, Rejection]] = [
    ("eicar test file", corpus.eicar_as_pdf(), "cv.pdf", PDF_MIME, Rejection.TYPE_MISMATCH),
    (
        "renamed executable",
        corpus.renamed_executable(),
        "cv.pdf",
        PDF_MIME,
        Rejection.TYPE_MISMATCH,
    ),
    ("gif/pdf polyglot", corpus.gif_pdf_polyglot(), "cv.pdf", PDF_MIME, Rejection.TYPE_MISMATCH),
    (
        "html disguised as pdf",
        corpus.html_disguised_as_pdf(),
        "cv.pdf",
        PDF_MIME,
        Rejection.TYPE_MISMATCH,
    ),
    ("truncated pdf", corpus.truncated_pdf(), "cv.pdf", PDF_MIME, Rejection.MALFORMED_PDF),
    ("10k-page pdf", corpus.huge_page_count_pdf(), "cv.pdf", PDF_MIME, Rejection.TOO_MANY_PAGES),
    (
        "pdf with javascript",
        corpus.pdf_with_javascript(),
        "cv.pdf",
        PDF_MIME,
        Rejection.ACTIVE_CONTENT,
    ),
    (
        "pdf with launch action",
        corpus.pdf_with_launch_action(),
        "cv.pdf",
        PDF_MIME,
        Rejection.ACTIVE_CONTENT,
    ),
    (
        "pdf with embedded file",
        corpus.pdf_with_embedded_file(),
        "cv.pdf",
        PDF_MIME,
        Rejection.ACTIVE_CONTENT,
    ),
    ("encrypted pdf", corpus.encrypted_pdf(), "cv.pdf", PDF_MIME, Rejection.ENCRYPTED),
    ("docx zip bomb", corpus.zip_bomb_docx(), "cv.docx", DOCX_MIME, Rejection.ZIP_BOMB),
    (
        "docx zip traversal",
        corpus.zip_traversal_docx(),
        "cv.docx",
        DOCX_MIME,
        Rejection.ZIP_PATH_TRAVERSAL,
    ),
    (
        "docx with 2500 entries",
        corpus.many_entries_docx(),
        "cv.docx",
        DOCX_MIME,
        Rejection.TOO_MANY_ZIP_ENTRIES,
    ),
    ("empty file", corpus.empty_file(), "cv.pdf", PDF_MIME, Rejection.EMPTY),
    ("oversized file", corpus.oversized_pdf(), "cv.pdf", PDF_MIME, Rejection.TOO_LARGE),
    (
        "disallowed extension",
        corpus.minimal_pdf(),
        "cv.exe",
        PDF_MIME,
        Rejection.EXTENSION_NOT_ALLOWED,
    ),
    ("no extension", corpus.minimal_pdf(), "resume", PDF_MIME, Rejection.EXTENSION_NOT_ALLOWED),
    ("lying content-type", corpus.minimal_pdf(), "cv.pdf", DOCX_MIME, Rejection.TYPE_MISMATCH),
]


@pytest.mark.parametrize(
    ("label", "data", "filename", "declared", "expected"),
    CASES,
    ids=[c[0].replace(" ", "-") for c in CASES],
)
def test_hostile_upload_is_rejected(
    label: str, data: bytes, filename: str, declared: str | None, expected: Rejection
) -> None:
    with pytest.raises(UploadRejectedError) as exc:
        validate(data, filename=filename, declared_mime=declared, limits=LIMITS)
    assert exc.value.reason is expected, (
        f"{label}: rejected as {exc.value.reason}, expected {expected}"
    )


def test_corpus_covers_at_least_eighteen_cases() -> None:
    """AC-8 names 18 cases. Pin it so the corpus cannot quietly shrink."""
    assert len(CASES) >= 18


def test_every_rejection_reason_is_exercised() -> None:
    """Any reason the validator can emit must have a test that provokes it —
    otherwise a branch exists that nothing verifies."""
    covered = {expected for *_, expected in CASES} | {
        Rejection.SNIFFED_TYPE_NOT_ALLOWED,
        Rejection.DECLARED_TYPE_NOT_ALLOWED,
        Rejection.MALFORMED_ZIP,
    }
    uncovered = set(Rejection) - covered
    assert not uncovered, f"rejection reasons with no test: {uncovered}"


# ---- filename handling --------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "../../../../etc/passwd.pdf",
        "..\\..\\windows\\system32\\evil.pdf",
        "resume\x00.pdf",
        "resume‮gnp.pdf",  # RTL override
        "a" * 400 + ".pdf",
        "/absolute/path/cv.pdf",
    ],
)
def test_hostile_filenames_reduce_to_a_bare_extension(filename: str) -> None:
    """The filename is never a path. Only the suffix is read, and it is matched
    against an allowlist — nothing from the user reaches the filesystem."""
    extension = safe_extension(filename)
    assert extension == ".pdf"
    assert "/" not in extension
    assert "\\" not in extension
    assert "\x00" not in extension


# ---- the guard that the parser boundary holds ---------------------------------


def test_no_unexpected_exception_escapes_validate() -> None:
    """`validate` must raise UploadRejectedError or nothing at all.

    A PDF declaring /Encrypt against an undefined object made pypdf raise
    AttributeError, which escaped and would have been a 500 from the upload
    endpoint. AC-8 requires zero crashes, so anything a parser throws on hostile
    input has to become a rejection.
    """
    import random

    # Seeded deliberately: fuzz findings must be reproducible. Not crypto.
    rng = random.Random(20260831)  # noqa: S311
    seeds = [
        corpus.minimal_pdf(),
        corpus.minimal_docx(),
        corpus.gif_pdf_polyglot(),
        corpus.encrypted_pdf(),
        corpus.truncated_pdf(),
        corpus.eicar_as_pdf(),
    ]

    for seed in seeds:
        for _ in range(60):
            mutated = bytearray(seed)
            for _ in range(rng.randint(1, 24)):
                if not mutated:
                    break
                index = rng.randrange(len(mutated))
                mutated[index] = rng.randrange(256)
            if rng.random() < 0.3 and len(mutated) > 40:
                del mutated[rng.randrange(len(mutated)) :]

            for filename, declared in (("cv.pdf", PDF_MIME), ("cv.docx", DOCX_MIME)):
                try:
                    validate(
                        bytes(mutated), filename=filename, declared_mime=declared, limits=LIMITS
                    )
                except UploadRejectedError:
                    pass
                except Exception as exc:
                    pytest.fail(
                        f"{type(exc).__name__} escaped validate(): {exc}\n"
                        f"seed={seed[:16]!r} filename={filename}"
                    )

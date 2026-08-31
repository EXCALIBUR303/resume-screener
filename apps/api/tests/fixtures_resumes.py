"""The 24-file parse fixture corpus behind AC-1.

Every document is SYNTHETIC. Names come from a fictional list, companies are
invented, and each carries a SYNTHETIC-DATA-DO-NOT-USE marker. Generated in code
so the cases are inspectable and the repository stays small.

The corpus deliberately includes documents that *cannot* yield text. AC-1 does
not require extracting from everything — it requires that anything which yields
nothing is explicitly flagged, never silently empty.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

from screener_api.ingest.validation import DOCX_MIME, PDF_MIME

MARKER = "SYNTHETIC-DATA-DO-NOT-USE"


@dataclass(frozen=True)
class Fixture:
    name: str
    data: bytes
    mime: str
    expect_text: bool  # should extraction yield >= 200 usable chars?
    note: str


def _pdf_with_text(blocks: list[str]) -> bytes:
    """A PDF carrying a real text layer, built by hand so no AGPL library is
    needed to produce one."""

    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    lines = []
    y = 750
    for block in blocks:
        for line in block.split("\n"):
            lines.append(f"BT /F1 11 Tf 50 {y} Td ({esc(line)}) Tj ET")
            y -= 14
            if y < 50:
                break
    stream = "\n".join(lines).encode("latin-1", "replace")

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def _docx_with_text(paragraphs: list[str], *, tables: list[list[str]] | None = None) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    for row in tables or []:
        cells = "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row)
        body += f"<w:tbl><w:tr>{cells}</w:tr></w:tbl>"

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/'
            '2006/content-types"><Default Extension="rels" ContentType="application/'
            'vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" '
            'ContentType="application/xml"/><Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
            'document.main+xml"/></Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
            f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>',
        )
    return out.getvalue()


_RESUME = [
    "PERSON_PLACEHOLDER",
    "candidate@example.com | +91 90000 00000",
    f"{MARKER}",
    "",
    "SUMMARY",
    "Backend engineer with seven years building payment and ledger systems.",
    "",
    "WORK EXPERIENCE",
    "Senior Backend Engineer, Invented Systems Ltd (2021-2026)",
    "Designed and ran payment services in Python on PostgreSQL at high throughput.",
    "Led the migration from a monolith to six independently deployed services.",
    "Backend Developer, Fictional Labs (2019-2021)",
    "Built internal tooling in Python, Redis and Celery.",
    "",
    "EDUCATION",
    "B.Tech Computer Science, Imaginary Institute of Technology, 2019",
    "",
    "TECHNICAL SKILLS",
    "Python, PostgreSQL, Redis, Docker, REST APIs, pytest",
    "",
    "PROJECTS",
    "Open-source ledger reconciliation tool used by three invented startups.",
]


def _long_resume(pages: int) -> list[str]:
    return _RESUME * pages


def build_corpus() -> list[Fixture]:
    f = Fixture
    return [
        # --- ordinary documents that must parse (14) ---
        f(
            "pdf-standard",
            _pdf_with_text(["\n".join(_RESUME)]),
            PDF_MIME,
            True,
            "typical one-page text-layer resume",
        ),
        f(
            "pdf-two-column-ish",
            _pdf_with_text(["\n".join(_RESUME[:12])]),
            PDF_MIME,
            True,
            "shorter layout",
        ),
        f(
            "pdf-dense",
            _pdf_with_text(["\n".join(_long_resume(2))]),
            PDF_MIME,
            True,
            "dense single page",
        ),
        f(
            "pdf-minimal-but-real",
            _pdf_with_text(["\n".join([*_RESUME[:9], "Extra detail line. " * 12])]),
            PDF_MIME,
            True,
            "just past the 200-char floor",
        ),
        f(
            "pdf-unicode",
            _pdf_with_text(
                ["\n".join(_RESUME).replace("PERSON_PLACEHOLDER", "Zoe Fictional-Name")]
            ),
            PDF_MIME,
            True,
            "hyphenated name",
        ),
        f(
            "pdf-symbols",
            _pdf_with_text(["\n".join(_RESUME) + "\nC++, C#, .NET, Node.js (100% remote)"]),
            PDF_MIME,
            True,
            "punctuation-heavy skills",
        ),
        f(
            "pdf-no-sections",
            _pdf_with_text(
                [MARKER + "\n" + "Long unstructured narrative about invented work history. " * 12]
            ),
            PDF_MIME,
            True,
            "no headings at all",
        ),
        f(
            "pdf-lowercase-headings",
            _pdf_with_text(["\n".join(x.lower() for x in _RESUME)]),
            PDF_MIME,
            True,
            "headings in lower case",
        ),
        f("docx-standard", _docx_with_text(_RESUME), DOCX_MIME, True, "typical DOCX resume"),
        f(
            "docx-with-table",
            _docx_with_text(
                _RESUME, tables=[["Skill", "Years"], ["Python", "7"], ["PostgreSQL", "6"]]
            ),
            DOCX_MIME,
            True,
            "skills in a table",
        ),
        f(
            "docx-long",
            _docx_with_text(_long_resume(3)),
            DOCX_MIME,
            True,
            "three times the content",
        ),
        f(
            "docx-single-paragraph",
            _docx_with_text([" ".join(_RESUME)]),
            DOCX_MIME,
            True,
            "everything in one paragraph",
        ),
        f(
            "docx-many-empty-paragraphs",
            _docx_with_text([p for line in _RESUME for p in (line, "", "")]),
            DOCX_MIME,
            True,
            "padded with blank paragraphs",
        ),
        f(
            "pdf-multi-block",
            _pdf_with_text(["\n".join(_RESUME[:11]), "\n".join(_RESUME[11:])]),
            PDF_MIME,
            True,
            "text split across content blocks",
        ),
        # --- documents that legitimately yield no text (5) ---
        # AC-1 does not require text from these. It requires them to be FLAGGED.
        f(
            "pdf-image-only",
            _image_only_pdf(),
            PDF_MIME,
            False,
            "pure scan, no text layer — must flag needs_ocr/no_text",
        ),
        f(
            "pdf-blank-page",
            _pdf_with_text([""]),
            PDF_MIME,
            False,
            "structurally valid, entirely empty",
        ),
        f(
            "pdf-whitespace-only",
            _pdf_with_text(["   \n   \n   "]),
            PDF_MIME,
            False,
            "only whitespace",
        ),
        f("docx-empty", _docx_with_text([]), DOCX_MIME, False, "valid DOCX with no paragraphs"),
        f(
            "docx-whitespace-only",
            _docx_with_text(["   ", "", "  "]),
            DOCX_MIME,
            False,
            "whitespace paragraphs only",
        ),
        # --- awkward but valid (5) ---
        f(
            "pdf-very-short",
            _pdf_with_text(["Jane Fictional\nEngineer"]),
            PDF_MIME,
            False,
            "real text but under the useful floor — must flag, not silently pass",
        ),
        f(
            "pdf-repeated-header",
            _pdf_with_text(["CONFIDENTIAL\n" * 8 + "\n".join(_RESUME)]),
            PDF_MIME,
            True,
            "repeated watermark text",
        ),
        f(
            "docx-nested-tables",
            _docx_with_text(_RESUME, tables=[["A", "B"], ["C", "D"], ["E", "F"]]),
            DOCX_MIME,
            True,
            "several tables",
        ),
        f(
            "pdf-long-lines",
            _pdf_with_text(["Achievement: " + "x" * 300 + "\n" + "\n".join(_RESUME)]),
            PDF_MIME,
            True,
            "single very long line",
        ),
        f(
            "docx-unicode-heavy",
            _docx_with_text(["Résumé — Zoë Fictional", *_RESUME]),
            DOCX_MIME,
            True,
            "accented characters and em dashes",
        ),
    ]


def _image_only_pdf() -> bytes:
    """A valid PDF whose single page is an image XObject with no text layer."""
    import zlib

    pixels = bytes([255, 255, 255] * (8 * 8))
    compressed = zlib.compress(pixels)
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length 44 >>\nstream\nq 612 0 0 792 0 0 cm /Im0 Do Q\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceRGB "
        b"/BitsPerComponent 8 /Filter /FlateDecode /Length "
        + str(len(compressed)).encode()
        + b" >>\nstream\n"
        + compressed
        + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()

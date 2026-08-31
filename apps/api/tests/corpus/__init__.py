"""Generators for the AC-8 malicious-upload corpus.

Built in code rather than committed as binaries: every case is inspectable, no
genuinely malicious binary lives in the repository, and `git clone` stays small.
The EICAR string is the industry-standard *harmless* antivirus test pattern.
"""

from __future__ import annotations

import io
import zipfile

# ---- benign baselines ---------------------------------------------------------


def minimal_pdf(pages: int = 1, *, extra: bytes = b"") -> bytes:
    """A structurally valid PDF with the requested page count."""
    objects: list[bytes] = []
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    objects.append(f"2 0 obj\n<< /Type /Pages /Count {pages} /Kids [{kids}] >>\nendobj\n".encode())
    for i in range(pages):
        objects.append(
            f"{3 + i} 0 obj\n<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 612 792] >>\nendobj\n".encode()
        )

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for obj in objects:
        offsets.append(out.tell())
        out.write(obj)
    out.write(extra)
    xref_at = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n".encode()
    )
    return out.getvalue()


def minimal_docx(*, entries: dict[str, bytes] | None = None) -> bytes:
    """A structurally valid minimal DOCX."""
    content = entries or {}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"><Default Extension="xml" '
            'ContentType="application/xml"/></Types>',
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Synthetic resume. '
            "SYNTHETIC-DATA-DO-NOT-USE</w:t></w:r></w:p></w:body></w:document>",
        )
        for name, blob in content.items():
            archive.writestr(name, blob)
    return out.getvalue()


# ---- the corpus ---------------------------------------------------------------

EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def eicar_as_pdf() -> bytes:
    return EICAR


def renamed_executable() -> bytes:
    """A Mach-O binary called cv.pdf."""
    return b"\xcf\xfa\xed\xfe" + b"\x00" * 512


def gif_pdf_polyglot() -> bytes:
    """Valid GIF header first, PDF body after — sniffs as GIF."""
    return b"GIF89a" + b"\x01\x00\x01\x00\x00\xff\x00," + minimal_pdf()


def zip_bomb_docx() -> bytes:
    """One entry that decompresses ~5000:1."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
        archive.writestr("bomb.bin", b"\x00" * (60 * 1024 * 1024))
    return out.getvalue()


def xxe_docx() -> bytes:
    """External entity referencing /etc/passwd in document.xml."""
    payload = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE root [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b"<w:document><w:body>&xxe;</w:body></w:document>"
    )
    return minimal_docx(entries={"word/evil.xml": payload})


def zip_traversal_docx() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
        archive.writestr("../../../../etc/cron.d/pwn", "* * * * * root sh -c id\n")
    return out.getvalue()


def many_entries_docx() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
        for i in range(2500):
            archive.writestr(f"e/{i}.xml", "<a/>")
    return out.getvalue()


def huge_page_count_pdf() -> bytes:
    return minimal_pdf(pages=5000)


def pdf_with_javascript() -> bytes:
    return minimal_pdf(extra=b"\n<< /Type /Action /S /JavaScript /JS (app.alert('x')) >>\n")


def pdf_with_launch_action() -> bytes:
    return minimal_pdf(extra=b"\n<< /Type /Action /S /Launch /F (/bin/sh) >>\n")


def pdf_with_embedded_file() -> bytes:
    return minimal_pdf(extra=b"\n<< /Type /EmbeddedFile /Subtype /application#2Fx-sh >>\n")


def encrypted_pdf() -> bytes:
    """A PDF with a well-formed /Encrypt dictionary, so `is_encrypted` isTrue.

    The first attempt pointed /Encrypt at an object that was never defined; pypdf
    raised AttributeError rather than reporting encryption, which is how the
    uncaught-exception hole in the validator was found.
    """
    enc = (
        b"9 0 obj\n<< /Filter /Standard /V 1 /R 2 /Length 40 "
        b"/P -1 /O <" + b"00" * 32 + b"> /U <" + b"00" * 32 + b"> >>\nendobj\n"
    )
    base = minimal_pdf(extra=enc)
    return base.replace(b"/Root 1 0 R", b"/Root 1 0 R /Encrypt 9 0 R")


def truncated_pdf() -> bytes:
    return minimal_pdf()[:120]


def empty_file() -> bytes:
    return b""


def oversized_pdf(size: int = 11 * 1024 * 1024) -> bytes:
    base = minimal_pdf()
    return base + b"\n%" + b"A" * (size - len(base) - 2)


def html_disguised_as_pdf() -> bytes:
    return b"<html><script>alert('xss')</script></html>"

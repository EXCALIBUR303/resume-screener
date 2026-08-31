"""Upload validation. Everything here treats the file as hostile.

Order matters: cheap structural checks run before anything parses the bytes, so
a zip bomb or a 10,000-page PDF is rejected before a library touches it.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from enum import StrEnum

import magic

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

ALLOWED: dict[str, set[str]] = {PDF_MIME: {".pdf"}, DOCX_MIME: {".docx"}}

# libmagic sometimes reports these for a valid DOCX, which is a ZIP underneath.
DOCX_SNIFF_ALIASES = {DOCX_MIME, "application/zip", "application/octet-stream"}


class Rejection(StrEnum):
    EMPTY = "empty_file"
    TOO_LARGE = "too_large"
    EXTENSION_NOT_ALLOWED = "extension_not_allowed"
    DECLARED_TYPE_NOT_ALLOWED = "declared_type_not_allowed"
    SNIFFED_TYPE_NOT_ALLOWED = "sniffed_type_not_allowed"
    TYPE_MISMATCH = "type_mismatch"
    MALFORMED_PDF = "malformed_pdf"
    TOO_MANY_PAGES = "too_many_pages"
    ENCRYPTED = "encrypted_document"
    MALFORMED_ZIP = "malformed_zip"
    ZIP_BOMB = "zip_bomb"
    TOO_MANY_ZIP_ENTRIES = "too_many_zip_entries"
    ZIP_PATH_TRAVERSAL = "zip_path_traversal"
    ACTIVE_CONTENT = "active_content"


class UploadRejectedError(Exception):
    def __init__(self, reason: Rejection, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else str(reason))
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ValidatedUpload:
    mime: str
    extension: str
    byte_size: int
    page_count: int | None
    sniffed_mime: str


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 10 * 1024 * 1024
    max_pages: int = 30
    zip_max_ratio: int = 100
    zip_max_entries: int = 2000
    zip_max_uncompressed: int = 100 * 1024 * 1024


def safe_extension(filename: str | None) -> str:
    """The user's filename is a display label. Only its suffix is consulted, and
    only against an allowlist — it never reaches the filesystem."""
    if not filename or "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower().strip()


def sniff(data: bytes) -> str:
    return str(magic.from_buffer(data[:8192], mime=True))


def validate(
    data: bytes, *, filename: str | None, declared_mime: str | None, limits: Limits | None = None
) -> ValidatedUpload:
    """Reject anything that is not unambiguously an allowed document."""
    limits = limits or Limits()

    if not data:
        raise UploadRejectedError(Rejection.EMPTY)
    if len(data) > limits.max_bytes:
        raise UploadRejectedError(
            Rejection.TOO_LARGE, f"{len(data)} bytes exceeds {limits.max_bytes}"
        )

    extension = safe_extension(filename)
    sniffed = sniff(data)

    # The three sources must agree. Extension and declared type are both under
    # the uploader's control; the sniff is the authority, and disagreement is
    # itself the signal.
    candidates = {m for m, exts in ALLOWED.items() if extension in exts}
    if not candidates:
        raise UploadRejectedError(Rejection.EXTENSION_NOT_ALLOWED, extension or "(none)")

    resolved = candidates.pop()

    if declared_mime and declared_mime.split(";")[0].strip() not in ALLOWED:
        raise UploadRejectedError(Rejection.DECLARED_TYPE_NOT_ALLOWED, declared_mime)
    if declared_mime and declared_mime.split(";")[0].strip() != resolved:
        raise UploadRejectedError(
            Rejection.TYPE_MISMATCH, f"declared {declared_mime}, extension implies {resolved}"
        )

    if resolved == PDF_MIME:
        if sniffed != PDF_MIME:
            raise UploadRejectedError(Rejection.TYPE_MISMATCH, f"sniffed {sniffed}, expected PDF")
        pages = _validate_pdf(data, limits)
    else:
        if sniffed not in DOCX_SNIFF_ALIASES:
            raise UploadRejectedError(Rejection.TYPE_MISMATCH, f"sniffed {sniffed}, expected DOCX")
        _validate_zip_container(data, limits)
        pages = None

    return ValidatedUpload(
        mime=resolved,
        extension=extension,
        byte_size=len(data),
        page_count=pages,
        sniffed_mime=sniffed,
    )


def _validate_pdf(data: bytes, limits: Limits) -> int:
    if not data.startswith(b"%PDF-"):
        raise UploadRejectedError(Rejection.MALFORMED_PDF, "missing %PDF- header")
    # A polyglot can carry a valid PDF trailer after other content; requiring the
    # header at offset 0 plus a trailer is a cheap structural sanity check.
    if b"%%EOF" not in data[-2048:] and b"%%EOF" not in data:
        raise UploadRejectedError(Rejection.MALFORMED_PDF, "missing %%EOF trailer")

    # Active content is stripped at parse time, but its presence is worth
    # refusing outright on an upload that should be a resume.
    for marker in (b"/JavaScript", b"/JS ", b"/Launch", b"/EmbeddedFile", b"/OpenAction"):
        if marker in data:
            raise UploadRejectedError(
                Rejection.ACTIVE_CONTENT, marker.decode(errors="replace").strip()
            )

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise UploadRejectedError(Rejection.ENCRYPTED, "password-protected PDF")
        page_count = len(reader.pages)
    except UploadRejectedError:
        raise
    except Exception as exc:
        # Deliberately broad. This is the boundary where a parser meets
        # attacker-controlled bytes, and the enumerable exception list is a
        # fiction: a PDF declaring /Encrypt against a missing object made pypdf
        # raise AttributeError, which escaped as a 500. Any parser failure on
        # hostile input is a rejection, never a crash. See
        # test_no_unexpected_exception_escapes_validate.
        raise UploadRejectedError(Rejection.MALFORMED_PDF, type(exc).__name__) from exc

    if page_count > limits.max_pages:
        raise UploadRejectedError(Rejection.TOO_MANY_PAGES, f"{page_count} > {limits.max_pages}")
    if page_count == 0:
        raise UploadRejectedError(Rejection.MALFORMED_PDF, "no pages")
    return page_count


def _validate_zip_container(data: bytes, limits: Limits) -> None:
    """DOCX is a ZIP. Check the central directory *before* extracting anything —
    a decompression bomb is only dangerous once you decompress it."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()

            if len(infos) > limits.zip_max_entries:
                raise UploadRejectedError(Rejection.TOO_MANY_ZIP_ENTRIES, f"{len(infos)} entries")

            total_uncompressed = 0
            for info in infos:
                name = info.filename
                if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
                    raise UploadRejectedError(Rejection.ZIP_PATH_TRAVERSAL, name[:80])

                total_uncompressed += info.file_size
                if total_uncompressed > limits.zip_max_uncompressed:
                    raise UploadRejectedError(
                        Rejection.ZIP_BOMB, f"uncompressed {total_uncompressed} bytes"
                    )
                if info.compress_size > 0:
                    ratio = info.file_size / info.compress_size
                    if ratio > limits.zip_max_ratio:
                        raise UploadRejectedError(
                            Rejection.ZIP_BOMB, f"ratio {ratio:.0f}:1 in {name[:60]}"
                        )

            if not any(i.filename == "word/document.xml" for i in infos):
                raise UploadRejectedError(
                    Rejection.MALFORMED_ZIP, "not a DOCX: no word/document.xml"
                )
    except UploadRejectedError:
        raise
    except Exception as exc:
        # Broad for the same reason as the PDF boundary. Fuzzing found
        # NotImplementedError("zip file version 12.4") escaping a BadZipFile-only
        # handler — the enumerable exception list is always incomplete when the
        # input is chosen by an attacker.
        raise UploadRejectedError(
            Rejection.MALFORMED_ZIP, f"{type(exc).__name__}: {exc}"[:80]
        ) from exc

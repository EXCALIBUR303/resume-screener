# ADR-0007 — Parser boundaries catch every exception

**Status:** accepted · **Date:** 2026-08-31

## Context

`validate()` is where library code meets attacker-chosen bytes. The first version caught an
enumerated list — `PdfReadError, ValueError, OSError, RecursionError` for PDFs,
`zipfile.BadZipFile` for DOCX. Broad `except Exception` is normally a smell, and ruff's `BLE001`
flags it.

Two escapes were found within minutes of each other:

| Input | Escaping exception | Consequence |
|---|---|---|
| PDF declaring `/Encrypt 9 0 R` against an undefined object | `AttributeError: 'NoneType' object has no attribute 'get_object'` (pypdf) | 500 from `POST /resumes` |
| Mutated DOCX with an unsupported compression version | `NotImplementedError: zip file version 12.4` (stdlib `zipfile`) | 500 from `POST /resumes` |

The second was found by the fuzz guard added after the first, on its very first run.

## Decision

At the two parser boundaries only, catch `Exception` and convert it to `UploadRejectedError`.
`UploadRejectedError` is re-raised first so genuine rejections keep their specific reason.
Suppressed with `# noqa: BLE001` and a comment naming the escape that motivated it.

## Rationale

The enumerable exception list is a fiction when the input is chosen by an attacker. A parser
fed hostile bytes can raise anything, and AC-8 requires zero crashes. "Reject" is always a
safe outcome here; "propagate" never is.

This applies **only** where untrusted bytes meet a parser. Elsewhere, catching broadly still
hides bugs, and the Semgrep config should not be loosened.

## Consequences

- `test_no_unexpected_exception_escapes_validate` fuzzes 720 mutations across six seed
  documents and fails if anything but `UploadRejectedError` escapes. It is the regression
  test for this whole class.
- Rejection reasons stay specific for real cases; only genuinely unexpected failures collapse
  into `malformed_pdf` / `malformed_zip`.

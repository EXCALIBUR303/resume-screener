# ADR-0002 — PDF library and project licence

**Status:** accepted · **Date:** 2026-08-31

## Context

PDF text extraction is the core of M3. The obvious candidate, **PyMuPDF**, is the fastest and
most capable option — and is licensed **AGPL-3.0**. AGPL is copyleft over a network boundary:
publishing a hosted service built on it obliges publishing the whole combined work under
AGPL.

The project's goal is a portfolio piece a hiring manager can read without friction, and that
Sid can reuse in future work.

## Decision

1. **Parsing:** `pypdf` + `pdfplumber` (both **MIT**), with `pytesseract` (Apache-2.0) for the
   OCR path. Not PyMuPDF.
2. **Project licence: Apache-2.0** — permissive, and unlike MIT it carries an explicit patent
   grant, which matters for anything AI-adjacent.

## Why not PyMuPDF

Speed is not the binding constraint. Resumes are 1–3 pages and the LLM call dominates the
pipeline at ~8.6 s (measured, ADR-0003); shaving 200 ms off extraction is invisible. Trading
the project's licence for that is a bad exchange.

Extraction *quality* on hard layouts (multi-column, tables) is the real risk, not speed.
Mitigation: the 24-file fixture corpus in M3 measures it, AC-1 gates it, and the OCR fallback
catches what the text layer misses.

## Consequences

- The `parse/` package hides the library behind a `TextExtractor` protocol, so this is
  reversible if `pdfplumber` proves inadequate on the corpus.
- **If that reversal ever happens, the project licence must change to AGPL-3.0 in the same
  PR.** This is the trap this ADR exists to prevent: adding a dependency is a licence
  decision, and it is invisible unless written down.
- `pdfplumber` pulls in `pdfminer.six`, which is slower and more memory-hungry. The M3 caps
  (30 pages, 60 s, `RLIMIT_AS` 1 GB) are sized with that in mind.

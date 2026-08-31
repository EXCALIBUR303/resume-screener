# ADR-0004 — Pin to Python 3.12

**Status:** accepted · **Date:** 2026-08-31

## Context

The development machine runs Python 3.14.6. M0 needs only FastAPI, Pydantic, SQLAlchemy and
structlog, all of which support it. M3–M5 need **spaCy** (with the `en_core_web_lg` model),
**Presidio**, **sentence-transformers** and **torch**.

Compiled ML wheels and pre-built spaCy models historically lag new CPython releases by
months. Discovering that at M3 means either a stalled milestone or building torch from
source.

## Decision

`requires-python = ">=3.12,<3.13"`, with `uv` fetching and managing the interpreter. The
container image is `python:3.12-slim` so local and CI and production agree.

## Why 3.12 rather than 3.13

3.12 has the widest wheel coverage across the whole intended dependency set today. The cost
is giving up newer language features, none of which this project needs.

## Consequences

- `uv venv --python 3.12` is required; `make bootstrap` does it, so this is invisible day to day.
- Revisit after M5, once the real ML dependency set is installed and its wheel support for
  newer interpreters can be checked rather than guessed.

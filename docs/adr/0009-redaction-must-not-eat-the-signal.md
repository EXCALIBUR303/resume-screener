# ADR-0009 — Redaction is a two-sided problem

**Status:** accepted · **Date:** 2026-08-31

## Context

AC-2 measures how much PII redaction *removes*. Nothing measured what it
*destroys*. Running one realistic resume through the finished pipeline showed
both failures at once — and the fixture corpus had passed cleanly.

| Symptom | Cause | Consequence |
|---|---|---|
| `Priya designed payment services…` kept the first name | Propagation matched only the exact full name; the corpus always used the full name in the body, real resumes do not | **PII leak** |
| `Redis, Docker` → `ORG_5` | NER returns them as ONE organisation span; the allowlist compared the whole string | Two skills destroyed |
| `Python on PostgreSQL` → `ORG_2` | Connector word `on` failed the technology allowlist | Two more skills destroyed |
| `SKILLS\nPython, PostgreSQL` → `ORG_2` | Allowlist ran *before* splitting the multi-line span, so the heading made the whole span fail | Skills destroyed |
| `(2021-2026)` → `PHONE_2` | Employment dates match the shape of a phone number | Years-of-experience input destroyed |
| One person → `PERSON_1`, `PERSON_2`, `PERSON_3` | Name parts got their own tokens | Model sees three candidates in one resume |

## Decision

Redaction is judged on **two** properties, and both are tested:

1. **Nothing identifying survives** — AC-2, measured at 100% on 60 seeded markers.
2. **Nothing scoreable is destroyed** — skills, employment dates and achievement
   detail must come through intact.

Concretely:

- A technology allowlist (111 entries) that NER can never override, checked
  **token-by-token after line splitting**, ignoring connector words.
- A digit floor on phone numbers, so date ranges are not phone-shaped.
- NER spans split at newlines before anything else, so a model span cannot
  swallow a more reliable pattern span. Patterns outrank the model.
- Name-part propagation, so a first name used alone is still redacted — sharing
  the parent's token, because one person is one token.

## Why this matters more than it looks

Over-redaction fails *silently and in the safe-looking direction*. A screener
that redacts "Redis" still returns a score; it is simply a worse score, for a
reason nobody can see. The deterministic half of the ranking is computed from
exactly the tokens this pipeline was destroying, so an over-eager redactor
would have quietly broken the scoring the whole project is built on.

## Consequences

- Five regression tests pin these cases, each naming the real document that
  exposed it.
- The allowlist is a maintenance surface: a new technology NER mistakes for a
  company will be redacted until someone adds it. That is an accepted, visible
  cost, preferable to trusting NER not to be confidently wrong.
- The fixture corpus was too kind. Test data drawn from the same template as the
  code's assumptions validates the assumptions, not the code.

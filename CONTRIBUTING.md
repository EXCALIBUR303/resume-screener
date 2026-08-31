# Contributing

## Before anything else

Read [`docs/BLUEPRINT.md`](docs/BLUEPRINT.md) §D — the anti-loophole rules. Every one names the
automated check that enforces it, because **a rule with no check is a wish**.

## The loop

```bash
make bootstrap    # once
make up           # stack
make check        # lint, types, tests, security scanners — what CI runs
```

## What CI will not let through

| Gate | Why it exists |
|---|---|
| A route with no authorization decision | `test_authz_matrix` enumerates the live route table. A new endpoint cannot merge without someone deciding who may call it. |
| A mutating route with no audit event | Same enumeration, applied to `POST`/`PUT`/`PATCH`/`DELETE`. |
| An edited prompt without a version bump | Stored scores reference a prompt hash. Editing `v1.md` makes past scores unexplainable — add `v2.md`. |
| A retrieval regression >0.03 nDCG@10 | `scripts/check_eval_regression.py` against the committed baseline. |
| Any High/Critical from bandit, semgrep, pip-audit, npm audit, trivy, gitleaks | — |
| A test fixture without `SYNTHETIC-DATA-DO-NOT-USE` | **Never commit a real person's resume — including your own.** |

## Writing tests

Two habits this project learned the hard way:

**Guard the guard.** A test that enumerates something must assert it found something. The
authorization matrix once passed while inspecting an empty list, and the eval harness once
reported `0.000` for a retriever that was simply broken.

**Test both directions.** Redaction must remove PII *and* preserve skills. Injection sanitisation
must remove the payload *and* keep the line above it. Every control that removes something needs
a test that it does not remove too much.

## Commits

Conventional Commits, enforced by pre-commit. Explain *why* in the body — the commit log is where
the reasoning lives, and several entries here are more useful than the diffs they describe.

## ADRs

Any decision that would be expensive to reverse gets one in `docs/adr/`. Record decisions that
turned out **wrong**, too, with the measurement that showed it — those are the most useful files
in the repository.

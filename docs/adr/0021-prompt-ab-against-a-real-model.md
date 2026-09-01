# ADR-0021 — Prompt A/B against a real model, and two metrics that were wrong

**Status:** accepted · **Date:** 2026-09-01 · **Completes the reasoning in** ADR-0019

## The experiment

`prompts/match_score/v2.md` is v1 plus a worked example of a correct response
and one added rule: copy the quote character-for-character from a **single**
bracketed chunk, and if you cannot find one, score the competency 0 rather than
approximating.

The hypothesis: models fail verbatim-quote requirements by paraphrasing and by
joining text across chunks, and *showing* the shape fixes more of it than
*describing* it does.

Run against `qwen3:8b` on the local Ollama host — 10 (resume, job) pairs spread
across archetypes, three runs per prompt, real model, no stub:

| prompt | zeroed rate | quotes / competency | verified rate | schema failures | median ms |
| --- | --- | --- | --- | --- | --- |
| v1 | **0.186** | 0.34 | 1.000 | 0 | 12459 |
| v2 | **0.000** | 0.58 | 1.000 | 0 | 10491 |

Per-run ranges: v1 `0.146 – 0.206`, v2 `0.000 – 0.000`. **Disjoint.** Every run
of v2 beat every run of v1.

v2 also came out faster, which was not predicted and is not the point — a
worked example makes the output shorter and more directly patterned, and the
median gap is one run's worth of noise wide. It is reported because it was
measured, not because it means anything.

## The first metric was measuring precision, and the difference was recall

The harness originally compared `verified_quote_rate`: of the quotes a model
cites, how many can be found in the document. It came out **1.000 for both
prompts** and the script reported "inconclusive".

That was a correct answer to the wrong question. Neither prompt makes the model
cite quotes it cannot support — the gate is not being fooled, it is going
hungry. v1 simply left competencies with *no* citable evidence at all, and the
pipeline zeroes those (ADR-0003), so they stop counting towards the score.

`zeroed_rate` — the fraction of competencies the evidence gate discarded — is
the number that actually changes a candidate's outcome, and it is what the
verdict now turns on. The mechanism is visible beside it: v2 produces nearly
twice the quotes per competency.

## The second metric was the wrong test

The first verdict compared the difference of means against the widest spread
within one prompt, the same noise-floor construction the fairness probe uses. It
called a visible effect inconclusive: on two runs v1 ranged 0.079 to 0.267, so
its own spread was as large as the gap to v2.

That test also gets **harder to pass as repeats increase**, because max-minus-min
grows with n. An estimator that punishes more evidence is the wrong estimator.

The verdict is range separation now: every observed run of one prompt beat every
observed run of the other. It is a statement about the runs actually performed,
carries no distributional assumption, and gets *stronger* with more repeats
rather than weaker.

## Adding a prompt file is a deploy

Both call sites loaded `latest_version(...)`. Prompt files are immutable once
committed (rule D-12), but "latest" is implicit — so writing `v2.md` in order to
run an experiment **changed what the worker scores with**, with no code change
and no review step. The experiment won, so the promotion was the right outcome.
It should not have been an accident.

`LLM_PROMPT_VERSION` pins the active version; unset means latest, so the default
behaviour is unchanged. What it buys is that "which prompt is running" can be a
decision rather than a consequence of which files happen to be in a directory.

## What this does not establish

Ten pairs, three runs, one model, a synthetic corpus. **v2's 0.000 does not mean
"never fails"** — it means it did not fail in thirty scored pairs. A different
model may behave differently; a worked example is exactly the kind of prompt
change whose benefit varies with model size.

The harness refuses to run against the stub provider rather than warning about
it. The stub derives its answer from a hash of the prompt, so it *would* produce
a difference between two prompts, and that difference would be pure noise
wearing a result's clothes. Exit code 2 and an explanation is better than a
number nobody should trust.

## Why this and not Langfuse

Recorded in ADR-0019, restated here because this is the artifact that replaces
it. Self-hosting Langfuse is ClickHouse, Redis, MinIO and a web tier, against a
constraint that forbids architecture with hidden ongoing costs. What it would
buy — comparing two prompt versions — is 250 lines that run on a laptop against
a model that is already there.

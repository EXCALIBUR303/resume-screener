# Evaluation

## What the labels are, and are not

The blueprint says "human-labelled relevance grades". **These labels are not
human judgments.** One person cannot meaningfully grade 400 resume × job pairs,
and pretending otherwise would be exactly the fabricated rigour this project
exists to avoid.

Instead the corpus is **constructed with known ground truth**. Each synthetic
resume is generated from an explicit profile — which skills it has, at what
depth, how many years, which section they appear in — and its grade against each
job description is *derived from that construction*, not from reading it back:

| grade | meaning |
|---|---|
| 3 | every required skill present, years requirement met |
| 2 | most required skills, or years slightly short |
| 1 | some overlap, clearly under-qualified |
| 0 | different discipline entirely |

## What that buys, and what it costs

**Buys:** ground truth that is exact, reproducible, and free of the annotator
drift a solo grader would introduce. A retrieval system that cannot find a
resume built to contain the requested skills is unambiguously wrong.

**Costs:** the labels encode *my* model of relevance, so the evaluation cannot
discover that the model is wrong. Real resumes are messier, and a system tuned
against constructed data can overfit to that tidiness. It measures whether the
system does what it was designed to do — not whether the design is right.

Any number produced here is reported with `n`, the corpus version, and this
caveat attached. Nothing in this directory supports a claim about real hiring.

## Layout

```
golden/     corpus.json      generated resumes + job descriptions + derived labels
            labels.jsonl     flattened (job_id, resume_id, grade) triples
suites/     injection.py     the AC-9 adversarial corpus
fairness/   variants.py      counterfactual resume sets, one protected signal per axis
            air.py           adverse impact ratio, and what it does not mean here
            run.py           the probe: real pipeline, real retrieval, stub model
baselines/  v1.json          committed metrics; CI compares against this
reports/    latest.json      retrieval metrics from the last run
            fairness.json    counterfactual probe from the last run
```

Regenerate with `make eval-data`, run with `make eval`.

## The counterfactual fairness probe

`make fairness` renders the same resume many times, changing exactly one signal correlated with a
protected attribute, and runs every rendering through the real `handle_score_job`.

Two questions, and they are not the same:

**Does redaction erase the signal?** For a *removable* axis — a name, a pronoun marker, a
personal-details block — every variant must reduce to **byte-identical redacted text**. That is
the strong claim and the one worth making, because everything downstream of redaction is a pure
function of that text and the job posting. Identical text is an identical score by construction,
with no measurement required. This is what `apps/api/tests/test_fairness.py` asserts, offline and
without a database.

**Does the score move anyway?** For axes that legitimately change the document — a career break, a
volunteering line — the scores are compared directly.

### It needs a control, and I found that out the hard way

The `control` axis is six renderings of the *same document*. Their spread is the noise floor, and
no axis effect at or below it means anything.

The first version had no control and reported six of seven axes as "differing" on nothing but
prompt variation. Three independent sources of per-run randomness reach the prompt: the nonce that
keys the untrusted-document fence, the resume id inside that fence, and the chunk ids, which are
fresh `uuid4` on every index. All three are correct in production and fatal in a harness. With
them controlled the noise floor is 0.000 and the numbers are readable.

This is the lesson of ADR-0015, learned again in the next harness I wrote.

### What it is not

Synthetic documents. Three base resumes per axis. The stub model. **It is not an applicant-flow
study, not a validated adverse-impact analysis, and passing it is not evidence the system is
fair.** The adverse impact ratio is computed because it is the number a reader looks for, and
reported with its limitations attached rather than omitted — see the module docstring in
`fairness/air.py`, which explains why it carries no information wherever invariance holds.

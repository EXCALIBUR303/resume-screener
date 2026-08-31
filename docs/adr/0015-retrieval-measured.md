# ADR-0015 — Hybrid retrieval, measured

**Status:** accepted · **Date:** 2026-08-31 · **Resolves the open question in** ADR-0011

## The bug the harness found first

`lexical_search` passed the whole job description to
`websearch_to_tsquery('english', :q)`, which **ANDs bare terms**. A chunk had to
contain every word of a paragraph. It matched nothing, for every query.

```
retriever    nDCG@10     P@5   Recall@20     MRR
lexical        0.000   0.000       0.000   0.000
```

This was live in production code — `handle_score_job` passes
`posting.description` straight into `hybrid_search` — so the lexical half of
"hybrid" retrieval had been contributing **exactly nothing** since M5. Neither
the unit tests nor the M5 comparison caught it: the unit tests queried single
terms like `Kubernetes`, which AND fine, and the M5 comparison used a
term-overlap proxy instead of the real function.

Fixed by converting free text to a disjunctive query before it reaches Postgres.

## The measurement

Golden set v1: 50 synthetic resumes × 8 job descriptions, 400 labelled pairs,
labels **derived from construction, not human judgment** (see `evals/README.md`).

| retriever | nDCG@10 | P@5 | Recall@20 | MRR |
|---|---|---|---|---|
| vector | 0.812 | 0.550 | 0.975 | 0.792 |
| **lexical** | **0.915** | **0.675** | 1.000 | 0.917 |
| hybrid | 0.907 | 0.650 | 1.000 | **1.000** |

## Reading it honestly

**M5's definition of done is still not met.** Hybrid does not beat both methods
on the primary metric: lexical wins nDCG@10 by 0.008 and P@5 by 0.025.

**But the corpus structurally favours lexical**, exactly as `evals/README.md`
predicted it might. The generator writes skill names literally into a skills
section, and the job descriptions use those same words. Real resumes paraphrase;
this corpus does not. The labels encode my model of relevance, so the evaluation
cannot discover that the model is wrong — and here that limitation is load
bearing, not theoretical.

**Hybrid wins the metric that matters most for a screener.** MRR 1.000 means a
relevant candidate was ranked first for **every** job description; vector
managed 0.792 and lexical 0.917. A recruiter reads from the top.

## Decision

Keep hybrid retrieval, on the MRR result and on robustness — the two retrievers
fail in different ways, and the corpus that flatters lexical is the same corpus
that cannot test paraphrase. Do **not** claim it improves relevance: the honest
sentence is

> On golden set v1 (n = 50 × 8, synthetic), hybrid retrieval ranked a relevant
> candidate first for every job description (MRR 1.000) while individual
> retrievers did not. It did not improve nDCG@10 over lexical search alone on
> this corpus, which is constructed in a way that favours exact term matching.

`evals/baselines/v1.json` is committed. CI compares against it.

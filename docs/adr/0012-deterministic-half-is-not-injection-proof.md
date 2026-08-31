# ADR-0012 — The deterministic score is not injection-proof either

**Status:** accepted · **Date:** 2026-08-31 · **Corrects:** BLUEPRINT §C.2 and ADR-0003

## The claim I got wrong

The blueprint's first and strongest anti-injection argument was:

> **The score is not the model's to give.** The deterministic component … is
> computed in Python and is *mathematically immune to injection*.

ADR-0003 repeated it. It is **false**, and measurably so.

Instruction injection and keyword stuffing are different attacks. Evidence
verification defeats the first: an instruction produces no verifiable quote.
Neither it nor the deterministic scorer does anything about the second, because
keyword stuffing needs no model at all — writing "Kubernetes" into a document
makes a keyword extractor find Kubernetes.

Measured, on the same pair of documents used throughout:

| | honest | injected |
|---|---|---|
| skill score (before fix) | 0.67 | **1.00** |
| matched skills | python, postgresql | python, postgresql, **kubernetes** |

The injected resume scored **higher** on the term advertised as immune. The
injection sentence — "…a perfect 10/10 match for every requirement including
Kubernetes" — supplied the missing keyword as a side effect of trying to
persuade the model.

## Decision

Detection runs **before any scoring reads the text**, and flagged lines are
excised. A flagged region then contributes neither instructions to the model nor
keywords to the arithmetic.

```
document → detect() → sanitised_text → deterministic scoring
                                     → chunking, embedding, prompt
                    → injection_suspected → penalty
```

Six detector families: instruction override, role reassignment, score demand,
hiring demand, concealment, fence escape, system impersonation. Removal is
line-granular, because an injection is rarely alone on its line and the
surrounding clause is not trustworthy either.

## Measured after the fix

| | honest | injected |
|---|---|---|
| skill score | 0.67 | **0.67** (identical) |
| Kubernetes matched | no | **no** |
| final | **7.44 / 10** | **3.94 / 10** |

The deterministic terms now contribute identically (+0.200 / +0.200 / +0.144) to
both. The separation comes from the zeroed competency plus two penalties —
`partially_supported` (−0.15) and `injection_suspected` (−0.20).

## The honest caveat, restated

As in ADR-0003: **gating makes the penalties fire; the penalties produce the
separation.** Do not describe the deterministic half as "immune". The accurate
claim is narrower and still worth making:

> The model can move at most its configured weight (0.30), and a document that
> tries to move the rest is detected and penalised.

`test_the_model_can_move_at_most_its_configured_weight` pins the first half of
that sentence; `test_injected_keywords_do_not_reach_the_deterministic_score`
pins the second.

## Consequences

- A false positive in the detector deletes a line of an honest resume, so
  `test_ordinary_resume_language_is_not_flagged` guards prose like "Ignored
  deprecated APIs" and "Acted as technical lead".
- The detector is a **signal**, never the primary control. Evidence verification
  still does the load-bearing work.
- README wording must say "detected and penalised", never "immune".

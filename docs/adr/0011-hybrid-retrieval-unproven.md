# ADR-0011 — Hybrid retrieval is built but **not yet proven better**

**Status:** accepted, with an unmet definition of done · **Date:** 2026-08-31

## Two claims in the blueprint, examined

### 1. "The tenant filter is applied before the ANN scan"

This conflated two separate things and was imprecise.

**Isolation is guaranteed by SQL semantics.** Every retrieval query carries
`org_id = :org`, and Postgres never returns a row that fails a predicate. That
holds whichever plan the optimiser picks.

**Pre- versus post-filtering is a *recall* question, not a correctness one.**
With an HNSW index pgvector may scan the index first and apply the predicate
after, which can return *fewer* than `k` rows — never rows from another tenant.
`hnsw.iterative_scan = relaxed_order` (pgvector ≥ 0.8) addresses the recall side.
The security property never depended on it.

Verified against a live database: Beta's chunks were given a vector **identical**
to Alpha's, so similarity alone would rank them equally. Alpha's search returns
only Alpha's rows.

### 2. "Hybrid beats either method alone" — M5's definition of done

**Not demonstrated.** Measured on 12 synthetic documents and 6 hand-labelled
queries:

| retriever depth fused | vector | lexical | hybrid |
|---|---|---|---|
| top-3 | 0.83 | 0.82 | 0.80 |
| top-5 | 0.83 | 0.82 | **0.84** |
| top-8 | 0.83 | 0.82 | 0.80 |
| top-10 | 0.83 | 0.82 | 0.80 |

Mean nDCG@5, n = 6 queries.

Hybrid ranges 0.80–0.84 **purely on the fusion-depth knob**. The spread produced
by an arbitrary parameter is larger than the gap between the methods, which is
conclusive evidence that six queries cannot distinguish them — not evidence that
fusion helps or hurts.

## Decision

1. **Do not tune the depth to top-5 because it wins.** Picking the parameter
   that flatters a 6-query sample is precisely the fake-metric behaviour this
   project exists to avoid. Depth stays at `limit * 2` on general reasoning.
2. **Keep hybrid retrieval**, on an argument that *was* demonstrated rather than
   on this measurement: lexical search finds exact rare tokens that dense
   retrieval misses. `test_lexical_search_finds_exact_tokens` shows a query for
   `Kubernetes` matching where semantic similarity alone is unreliable, and the
   fusion tests show neither method's unique finds are dropped.
3. **Defer the real measurement to M7**, which builds the golden set the
   blueprint specifies — 50 resumes × 8 job descriptions with graded relevance —
   and runs against actual `ts_rank_cd` rather than the term-overlap proxy used
   here.
4. **M5's DoD is recorded as unmet.** The milestone ships because the
   implementation and the isolation guarantee are sound; the ranking claim is
   not yet supportable and is not being made.

## Why this is written down rather than quietly fixed

The honest failure mode for a portfolio project is a README claiming "hybrid
retrieval improves relevance" backed by nothing. The measurement was run, it
came back flat, and the flat result is more useful than a tuned one: it says
exactly what evidence is still missing and where it will come from.

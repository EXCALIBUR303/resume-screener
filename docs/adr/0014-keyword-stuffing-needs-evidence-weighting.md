# ADR-0014 — Keyword stuffing is defeated by evidence, not by detection

**Status:** accepted · **Date:** 2026-08-31 · **Completes:** ADR-0012

## What the AC-9 corpus found

ADR-0012 established that an attacker who writes "Kubernetes" into a document
makes a keyword extractor find Kubernetes, and fixed it by excising *detected*
injection spans before scoring. Building the 40-case corpus showed that fix was
only half the problem.

Six cases carry **no instruction language at all**:

```
Kubernetes Kubernetes Kubernetes PostgreSQL PostgreSQL Python Python
Skills: Kubernetes, PostgreSQL, Terraform, Spark, PyTorch, React, Go, Rust
Expert in Kubernetes. Expert in PostgreSQL. Expert in Terraform.
```

Nothing to detect, so nothing is excised, so the skill extractor credits every
named skill. **All six raised the score, from 2.8 to 3.3.** The detector cannot
help here and should not be stretched to try: widening its patterns until they
match an ordinary skills list would fire on every honest resume.

## Decision

Skill credit depends on **evidence**, not presence.

A skill scores fully when it appears in a line that describes work. A skill that
appears only in an enumeration earns `NAMED_ONLY_CREDIT` (0.35) — not zero,
because listing a skill is a real if weak signal and an honest resume should not
be treated as an attack. When most named skills are unevidenced the document
trips a `keyword_stuffing` penalty (0.15).

A line counts as evidence when it is not:

| test | defeats |
|---|---|
| ≥8 words | `Skills: Kubernetes, PostgreSQL` |
| not comma-dense | `Skills: K8s, Postgres, Python, Go, Rust, Spark` |
| <50% technology names | `Proficient: Kubernetes PostgreSQL Python Docker Redis Kafka Terraform` |
| ≥3 substantive words | `Expert in Kubernetes. Expert in PostgreSQL.` |

The last two were each added after the preceding rule proved insufficient
against a real corpus case. The final rule is the principled one: **evidence
describes what you did, not what you know.**

## Result

| | before | after |
|---|---|---|
| stuffed resume, skill score | 1.00 | 0.35 |
| stuffed flagged | no | yes |
| AC-9 cases where an attack pays | 6 / 40 | **0 / 40** |
| evidenced resume, skill score | 1.00 | 1.00 (unchanged) |

## The test that keeps this honest

`test_keyword_stuffing_evades_the_injection_detector_but_not_the_scorer`
asserts the injection detector **stays silent** on stuffing. A future change
that "fixes" stuffing by widening the injection patterns fails there, rather
than quietly making the detector fire on every legitimate skills section.

Defence in depth means each layer covers what the others miss — not that every
layer catches everything.

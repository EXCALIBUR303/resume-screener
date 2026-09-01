# ADR-0023 — Institutions redacted by shape, not by whether a model knew the name

**Status:** accepted · **Date:** 2026-09-01 · **Closes the open limitation in** ADR-0017

## The limitation ADR-0017 recorded and did not fix

> *"NER catches `Stanford University` and not `Imaginary Institute`. Institution
> redaction is inconsistent, and an institution is a proxy for background."*

It was recorded as measured-not-solved. Measuring it properly made it worse than
the sentence suggests. Ten institutions on an otherwise identical education line:

| institution | before |
| --- | --- |
| Stanford University | `B.Tech Computer Science, ORG_1` |
| Imaginary Institute of Technology | `B.Tech Computer Science, Imaginary ORG_1` |
| Nowhere Polytechnic | `B.Tech Computer Science, Nowhere Polytechnic` |
| Placeholder School of Engineering | `B.Tech Computer Science, Placeholder School of Engineering` |
| Example College | `ORG_1` |

**Two of ten survived untouched. Five distinct shapes across ten inputs.** And
the last row is a second failure: where NER spanned the whole line, the
**degree** went with the institution — the exact inversion ADR-0017 claimed to
have fixed. That fix only worked when NER isolated the degree; it did nothing
when the span covered both.

## What it was worth

The fairness probe had no institution axis, so none of this was visible to it.
Adding one and removing the rule as a negative control:

```
institution   value hidden: False   d value: 0.412   VALUE MOVES THE SCORE
```

**0.412 on a 0–1 scale** — larger than any effect this harness has measured, and
it was sitting in the pipeline undetected because nobody had asked the question.
That is the same lesson as ADR-0017's: a probe only finds what it has an axis
for.

## Decision

Match institutions **by shape, in the pattern layer**, rather than leaving them
to NER:

```
(Capitalised words){1,4} (University|College|Institute|School|Academy|Polytechnic|Seminary)
                                          [optional "of <Subject>"]
```

Two consequences fall out of putting it in the pattern layer specifically:

1. **Determinism.** Which candidates get their institution removed no longer
   depends on a statistical model's coverage. All ten now reduce to one shape.
2. **The degree survives.** Pattern beats NER in the merge (ADR-0017), so the
   NER span that used to swallow `B.Tech Computer Science, Example College`
   whole is rejected for overlapping this one. 10/10 degrees preserved, up from
   7/10.

Emitted as `ORG`, sharing the employer numbering deliberately. A distinct entity
would make the redacted text differ depending on *which layer* caught the
institution — the same invariance failure this is fixing.

## A third failure, found on the way

`B.Tech CS` was being redacted as a `PERSON`. `is_degree_phrase` requires every
token to be degree vocabulary, and `CS` was not in it — nor were `ECE`, `AI`,
`ML` or the unpunctuated `BTech`. So the abbreviated forms most common on Indian
and European resumes lost their degree while `B.Tech Computer Science` kept its
own. A redaction rule whose behaviour depends on how a candidate abbreviates
their qualification is the same class of problem as one that depends on whether
NER knows their university.

## What this does not fix

Institutions with no marker word — `MIT`, `Caltech`, `IIT Bombay`, `Oxford`.
Those still depend on NER, which happens to catch all three of the ones tested
here, and that is luck rather than a property. A name list would be
unmaintainable and would fail on exactly the institutions least represented in
it, which is the wrong direction for this particular control.

The honest statement: **the common case is now deterministic, the uncommon case
is still NER-dependent, and the fairness probe has an axis that will notice if
that changes.**

## Consequences

- 32 new tests. The institution axis is in the counterfactual corpus and in the
  CI gate, and I verified the gate fails with the rule removed rather than
  assuming it would.
- The README's thesis image changed: the education line now reads
  `B.Tech Computer Science, ORG_2` where it previously leaked `Imaginary`.
- Slightly more aggressive redaction. `test_the_institution_rule_does_not_fire_on_ordinary_prose`
  pins the boundary: lowercase "school" is a noun, and over-redaction is a
  different failure rather than a safe direction (ADR-0009).

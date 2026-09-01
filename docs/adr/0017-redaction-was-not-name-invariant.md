# ADR-0017 — Redaction was not name-invariant, and eight other things the fairness probe found

**Status:** accepted · **Date:** 2026-09-01 · **Extends** ADR-0009 · **Repeats the lesson of** ADR-0015

## The finding that started it

Two resumes, identical in every substantive respect, differing only in the
candidate's given name. The same phone number in the same position:

```
Priya  Placeholder   ->  EMAIL_1 | PHONE_1
Alex   Placeholder   ->  EMAIL_1 PERSON_2 POSTAL_US_1 POSTAL_US_2
```

The phone pattern matched `+91 90000 00000` in **both** cases. What differed was
that spaCy, for some names and not others, returned an extra `PERSON` span
covering the five characters `"| +91"` at offset 34. `_merge` sorted spans by
start position and dropped anything overlapping the span before it, so the
five-character fragment at 34 suppressed the fifteen-character phone match at
36. With the phone match gone, the two weaker `POSTAL_US` patterns picked up
`90000` and `00000` on their own.

The docstring said **"longest span wins, ties broken by layer priority."** The
code did not do that. It did "first-starting span wins", and the difference only
becomes visible when a spurious short span starts two characters early.

No PII escaped — the digits were removed either way. What escaped was
**invariance**: the redacted text is the exact string that gets chunked,
embedded, retrieved and interpolated into the prompt, so two candidates with the
same experience got different documents, different embeddings and different
scores *because of their names*.

## Decision

`_merge` orders by layer reliability first, then length, then position, and
accepts a span only if it overlaps nothing already accepted. Deterministic
patterns beat NER, which is what the four-layer design says it does. The overlap
check uses `bisect` against a sorted list rather than a scan, because span input
is attacker-controlled and a quadratic check is a cheap way to stall the parse
worker.

## Eight more, found by building the measurement

The harness that caught the above (`evals/fairness/`) kept going.

**1 — The protected-attribute layer was eating security vocabulary.** Every term
matched bare, so on an application-security resume:

| in | out |
| --- | --- |
| `man-in-the-middle attacks` | `GENDER_1-in-the-middle attacks` |
| `cache miss rate` | `cache GENDER_1 rate` |
| `Disabled TLS 1.0` | `DISABILITY_1 TLS 1.0` |
| `Single Sign-On with SAML` | `MARITAL_1 Sign-On with SAML` |
| `single point of failure` | `MARITAL_1 point of failure` |

This is ADR-0009 ("redaction must not eat the signal") recurring in the one
layer ADR-0009 never audited. `man`, `miss`, `single` and `disabled` now need a
demographic cue within 40 characters — the same context-window technique the
graduation-year rule already used.

**2 — `POSTAL_US` was `\b\d{5}\b`.** On an engineering resume five-digit numbers
are almost all achievement metrics: `50000 concurrent connections`, `12000 ms`,
`10000 records nightly`. All redacted. Now context-gated on an address cue or a
US state abbreviation immediately before.

**3 — Three of twelve gender terms could never fire.** The table wraps every
term in `\b...\b`, and a `\b` after a period requires a word character next.
`mr.`, `mrs.` and `ms.` therefore matched `Ms.Alex` and never `Ms. Alex`, which
is how a resume is actually written. `age:` had the identical defect. **A
control that cannot fire is worse than no control**, because the table implies
coverage it does not have. Both are shaped patterns now, and the dead entries
were deleted rather than left in place looking like coverage.

**4 — The label was redacted and the value survived.** `Nationality: Indian`
produced `NATIONALITY_1: Indian`. The word naming the attribute was removed and
the attribute itself went to the model.

**5 — The degree was redacted and the institution survived.** NER classifies
`B.Tech Computer Science` as an ORGANIZATION. Exactly inverted: the degree is
the qualification signal and the institution is the proxy for background.

**6 — The reason for a career break reached the model, and moved the score.**

| reason | mean score |
| --- | --- |
| sabbatical | 0.862 |
| caregiving | 0.799 |
| parental leave | 0.599 |

Under the stub provider the *direction* is a hash artefact and means nothing.
The *sensitivity* is real and means a great deal: the score is a function of why
someone stepped away from work, and that reason tracks sex, age and disability.
The gap itself is legitimate information and stays, dates included. The
parenthetical reason is removed — for **every** reason including "sabbatical",
because redacting only the protected ones would make their absence the signal.

**7 — Pseudonymising a protected attribute leaks the disclosure.** A resume
declaring pronouns produced `EMAIL_1 | PHONE_1 | GENDER_1`; one that did not
produced `EMAIL_1 | PHONE_1`. The value was hidden and the *act of disclosing*
was not — and who volunteers that line correlates with the attributes the layer
exists to remove.

Pseudonymisation is the right default because a recruiter re-hydrates `PERSON_1`
to see who they are looking at. **Nobody ever re-hydrates a candidate's
religion.** For protected entities the token buys nothing and costs a channel,
so they are now deleted outright, along with the separator that framed them and
the line if nothing else was on it.

**8 — `re.I` widens `[A-Z]` to match lowercase.** The new honorific rule was
`\b(?:mr|ms|miss)\.?(?=\s+[A-Z])` under `re.IGNORECASE`, so `cache miss rate`
satisfied "honorific followed by a capitalised name" and the unit was deleted.
Scoped off with `(?-i:[A-Z])`. A case-insensitive character class is a quiet way
to widen a pattern far past what it appears to say.

## The harness needed a determinism test before its numbers meant anything

ADR-0015 recorded that lesson for retrieval. I wrote the next harness and made
the same mistake.

The first run reported six of seven axes as "DIFFERS". Then the control — six
**byte-identical** documents — spread by 0.412, which was exactly as wide as the
largest axis effect on the board. The `name` axis, whose variants redact to the
same bytes, was among the six. Three independent sources of per-run variation reach the prompt:

1. the per-request `nonce` that keys the untrusted-document fence,
2. the resume id, interpolated into the same fence,
3. the chunk ids, which are fresh `uuid4` on every index.

All three are correct in production and fatal in a harness. Controlled by
pinning the nonce (an optional argument on `handle_score_job`, defaulting to
random and used by nothing in the application), reusing one resume row per
counterfactual set, and deriving chunk ids in the harness. The noise floor went
to **0.000** and the results became readable.

**A consequence worth naming: a stored score is reproducible only up to its
prompt *template*.** `matches.prompt_hash` identifies the template, not the
rendered prompt, and re-scoring the same resume can legitimately return a
different number. That is the cost of the nonce, and the nonce is worth it.

## Result

| axis | value hidden | disclosure hidden | Δ value |
| --- | --- | --- | --- |
| name | yes | yes | 0.000 |
| gender_marker | yes | yes | 0.000 |
| personal_details | yes | yes | 0.000 |
| graduation_year | yes | yes | 0.000 |
| affinity_group | yes | no | 0.000 |
| location | yes | no | 0.000 |
| career_gap | yes | no | 0.000 |

"Disclosure hidden: no" on the last three is honest and expected. An affinity
group, a location line and a career break are real extra content; the system can
see a line is there. What it cannot see is which one.

## What this does not establish

The corpus is synthetic, `n` is three base resumes per axis, and the LLM is the
stub. **This is a counterfactual-invariance test on invented documents. It is
not an applicant-flow study, it is not a validated adverse-impact analysis, and
passing it is not evidence that the system is fair.** The adverse-impact ratio
is computed and reported because it is the number a reader looks for, with its
limitations attached — where invariance holds it is 1.0 by arithmetic necessity
and carries no information the invariance result did not already carry.

The residual limitation the probe could not fix: **NER catches
`Stanford University` and not `Imaginary Institute`.** Institution redaction is
inconsistent, and an institution is a proxy for background. Making it consistent
needs either a position-based rule inside the education section or a model that
is better at organisations than `en_core_web_sm`. Recorded, not solved.

> **Closed 2026-09-01 by [ADR-0023](0023-institutions-redacted-by-shape.md).**
> Matched by shape in the pattern layer instead. Measuring it first showed it was
> worse than this paragraph says: two of ten institutions survived untouched,
> the ten produced five different shapes, and where NER spanned the whole
> education line the *degree* was destroyed with it — which is the failure this
> ADR claimed to have fixed, working only when NER isolated the degree. Adding
> an `institution` axis to the probe put a number on it: **0.412**, the largest
> effect this harness has measured.

## Consequences

- One optional argument added to `handle_score_job`. Production behaviour
  unchanged; the default is still a fresh random nonce.
- Protected entities no longer appear as tokens in redacted text. Anything
  asserting on `GENDER_1` would break; nothing did.
- 62 new tests, all offline and database-free. They assert on redacted **text**
  rather than on measured scores, which is the stronger claim: everything
  downstream is a pure function of that text and the job posting.

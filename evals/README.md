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
baselines/  v1.json          committed metrics; CI compares against this
reports/    <sha>.md         per-run output
```

Regenerate with `make eval-data`, run with `make eval`.

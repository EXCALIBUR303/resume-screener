# Secure Multi-Modal AI Resume Screener

A resume screening system that **redacts before it reasons**, scores with evidence it can
prove, and survives a resume that tries to talk to the model.

[![license](https://img.shields.io/badge/license-Apache--2.0-2E6B5B)](LICENSE)

> **Status.** 488 tests, and `ci`, `security` and `eval` run green on every push to `main`.
> The badge above is static (it states the licence, which does not change); the workflows
> in `.github/workflows/` are the real signal.

> **Decision support only.** This is a portfolio and research project. It does not make hiring
> decisions, it has not been audited for bias, it is not a compliant automated employment
> decision tool, and it must not be pointed at real candidate data. See
> [Limitations](#limitations-and-non-goals).

---

## The thesis, in one image

```
UPLOADED BY THE RECRUITER                    WHAT THE MODEL ACTUALLY RECEIVES
─────────────────────────────────────────    ──────────────────────────────────────────
Priya Ramanathan                             PERSON_1
priya.ramanathan@example.com | +91 98765…    EMAIL_1 | PHONE_1 | PROFILE_1
Bengaluru, India | Female, married |         LOCATION_1, LOCATION_2 | DOB_1 |
  D.O.B: 12/04/1997 | PAN ABCDE1234F           PAN PAN_1

WORK EXPERIENCE                              WORK EXPERIENCE
Senior Backend Engineer,                     Senior Backend Engineer,
  Invented Systems Ltd (2021-2026)             ORG_1 (2021-2026)
Priya designed payment services in           PERSON_1 designed payment services in
  Python on PostgreSQL at 12k req/s            Python on PostgreSQL at 12k req/s
Ramanathan led the migration from a          PERSON_1 led the migration from a
  monolith to six services                     monolith to six services

EDUCATION                                    EDUCATION
B.Tech Computer Science,                     B.Tech Computer Science,
  Imaginary Institute of Tech., 2019           Imaginary ORG_2

TECHNICAL SKILLS                             TECHNICAL SKILLS
Python, PostgreSQL, Redis, Docker,           Python, PostgreSQL, Redis, Docker,
  Kubernetes, REST APIs, pytest                Kubernetes, REST APIs, pytest
```

15 identifiers removed. **Every skill, employment date and achievement intact.** Redaction runs
inside a worker with no network access, before anything is embedded, prompted, indexed or logged.

Note what is *not* on the right: `Female, married` left no token at all, and neither did the
graduation year. Protected attributes are **deleted, not pseudonymised** — a `GENDER_1` token
would hide which gender while advertising that the candidate disclosed one, and who volunteers
that line correlates with the very attribute being removed. Names and contact details keep their
tokens because a recruiter re-hydrates those to see who they are looking at; nobody re-hydrates a
candidate's religion. ([ADR-0017](docs/adr/0017-redaction-was-not-name-invariant.md))

Reproduce it yourself: `make redact-demo`

---

## The problem

Keyword-matching ATS ranking is bad at its job — it cannot tell a candidate who *has* used
Kubernetes from one who *typed* the word. The obvious fix, handing resumes to a language model,
introduces worse problems:

- A resume is **attacker-controlled input**. It can contain instructions aimed at the model.
- Resumes carry **exactly the attributes hiring must not consider** — age, gender, marital
  status, nationality.
- A model will produce a confident number whether or not it has grounds for one.

This project treats all three as engineering problems with testable answers.

---

## What it does

> **"Multi-modal" means text and scanned image**, not audio or video. A resume can arrive as
> extractable text or as a scan that goes through OCR, and both take the same redaction and
> scoring path. Audio interview answers are designed in
> [the blueprint](docs/BLUEPRINT.md#m14--v2-depth--3040-h--nice--v2) and **not built**;
> `FEATURE_AUDIO_ANSWERS` is `false` and nothing behind it exists.

```
upload ──▶ sniff + cap ──▶ quarantine ──▶ promote ──▶ enqueue
                                                        │
   ┌────────────────────────────────────────────────────┘
   ▼  worker-parse   ── NO NETWORK, read-only rootfs, non-root ──
 extract ─▶ OCR? ─▶ sections ─▶ REDACT ─▶ pseudonymise ─▶ persist
                                                        │
   ┌────────────────────────────────────────────────────┘
   ▼  worker-ai      ── model egress only ──
 chunk ─▶ embed ─▶ index ─▶ retrieve ─▶ score ─▶ verify evidence ─▶ rank
```

| | |
|---|---|
| **Ingestion** | PDF and DOCX, magic-byte sniffing, OCR fallback for scans, 18-case malicious-upload corpus |
| **Privacy** | Four-layer redaction, pseudonymised so a recruiter still sees a person, AES-256-GCM envelope encryption, complete erasure |
| **Ranking** | Deterministic skill/experience terms + hybrid retrieval + a capped model term, every contribution itemised |
| **Web UI** | Sign-in, resume upload, role creation, ranked candidates, and an explanation drawer showing every term with who computed it |
| **Interview copilot** | Questions grounded in verified gaps, with anchored rubrics; ungrounded or unlawful questions are rejected |
| **Operations** | Prometheus metrics with five alert rules, a Grafana dashboard, cross-process tracing, and a [runbook](docs/runbook.md) whose procedures have actually been run |
| **Security** | Tamper-evident audit chain, tenant isolation proven against a live database, 40-case injection corpus |

---

## Explainability

A score is never a bare number:

```
1. CANDIDATE_621598CE   8.62/10
     skill       0.30 x 1.00 = +0.300  [python]
     experience  0.20 x 1.00 = +0.200  [python]
     semantic    0.20 x 0.50 = +0.100  [python]
     rubric      0.30 x 0.62 = +0.188  [model]
     Python       claimed=4 verified=1/1 effective=4
     Kubernetes   claimed=0 verified=0/0 effective=0

4. CANDIDATE_1A00A10E   0.00/10   [INJECTION SUSPECTED]
     penalty injection_suspected  -0.200
     penalty partially_supported  -0.150
     Python       claimed=4 verified=0/1 effective=0   <-- ZEROED
```

Every term shows its weight, its value, and **who computed it**. The model's total influence is
capped at 0.30 and [asserted by a test](apps/api/tests/test_scoring.py).

---

## Security design

The full threat model is in [`docs/BLUEPRINT.md`](docs/BLUEPRINT.md). The parts that earned their
keep:

**The parse worker has no network at all.** It is the only process that touches
attacker-controlled bytes, so it is given nothing to exfiltrate with — `network_mode: none`,
read-only root filesystem, no capabilities, non-root, 1 GB address-space cap. It reaches Postgres
over a shared unix socket. Verified empirically, not asserted:

```
1.1.1.1:53   blocked (OSError)      db:5432   blocked (gaierror — DNS cannot resolve it)
touch /probe Read-only file system  id        uid=10002(worker)
unix socket  job_queue reachable
```

**Prompt injection is defeated by evidence, not by detection.** A model *will* be fooled —
[ADR-0003](docs/adr/0003-evidence-gating-granularity.md) measured `qwen3:8b` obeying an injected
resume completely. Every competency the model claims must cite a quote that appears **verbatim**
in the source, checked per competency. A fabricated claim contributes zero.

**The audit log is tamper-evident.** `hash = sha256(prev_hash ‖ canonical_json(event))`, with the
database denying `UPDATE` and `DELETE` outright. It caught a forged row planted during testing
that nothing had told it about.

**Erasure is complete, and the chain survives it.** Deleting a candidate removes every row, the
encrypted PII map and the blob on disk, leaving a hash-only tombstone. The chain still verifies.

**Authorization is scoped by attribute, not only by role.** A hiring manager holds `MATCH_READ`,
which is a role-level answer to a resource-level question — so one could read the ranked
candidates for every position in the tenant. Access is now a row in `job_assignments`, applied as
a `WHERE` clause. Filtering after the query would still leak the count and burn the page budget
([ADR-0020](docs/adr/0020-attribute-scoped-match-access.md)).

**Webhook egress is treated as hostile.** A webhook URL is typed in by a tenant and fetched by our
infrastructure. HTTPS only, every resolved address checked against private and link-local ranges,
no redirects, and a separate process holding a key that cannot decrypt a PII map. The DNS
rebinding window is **not** closed, and
[ADR-0018](docs/adr/0018-transactional-outbox-and-webhook-egress.md) says why the standard fix was
measured and rejected rather than adopted.

**Every build produces an SBOM.** Three CycloneDX documents per security run — both images and the
repository's pinned dependency graphs — kept as artifacts, so "was this build affected by CVE-X"
is answerable after the fact. Image signing (cosign keyless, digest not tag, SBOM attested rather
than attached) is written in `release.yml` and **has never run**: signing is only meaningful for an
artifact in a registry, and publishing one is your decision to make.

---

## Evaluation

Golden set v1: 50 synthetic resumes × 8 job descriptions = 400 pairs.

| retriever | nDCG@10 | P@5 | Recall@20 | MRR |
|---|---|---|---|---|
| vector | 0.812 | 0.550 | 0.975 | 0.792 |
| lexical | **0.927** | **0.675** | 1.000 | 0.938 |
| hybrid | 0.902 | 0.675 | 1.000 | **0.938** |

**Read this honestly.** Labels are *derived from construction*, not human judgment — see
[`evals/README.md`](evals/README.md) for what that buys and costs. Hybrid retrieval **does not**
beat lexical on nDCG@10 here; the corpus writes skill names literally and so favours exact
matching. Hybrid does not beat lexical on MRR either once the ordering is made reproducible — they tie
at 0.938. The earlier MRR 1.000 for hybrid was an artefact of unstable tie-breaking, not a
result. **On this corpus hybrid retrieval is not measurably better than lexical search alone.**
It is kept for the reason given in [ADR-0015](docs/adr/0015-retrieval-measured.md), not because
the numbers support it.

### Counterfactual fairness probe

`make fairness` renders 123 variants of the same handful of resumes, changing **one**
protected-attribute signal at a time, and runs every one through the real scoring pipeline.

| axis | value hidden | disclosure hidden | Δ score between values |
|---|---|---|---|
| name | yes | yes | 0.000 |
| gender marker | yes | yes | 0.000 |
| personal details | yes | yes | 0.000 |
| graduation year | yes | yes | 0.000 |
| affinity group | yes | no | 0.000 |
| location | yes | no | 0.000 |
| career break reason | yes | no | 0.000 |

The first run of this probe found nine defects, including one where **the same phone number was
redacted correctly for one candidate name and shattered into two fake postal codes for another** —
so two identical resumes were embedded, retrieved and scored differently because of the
candidate's name. [ADR-0017](docs/adr/0017-redaction-was-not-name-invariant.md) has all nine.

**Read this honestly.** Synthetic documents, three base resumes per axis, stub model. It shows this
pipeline does not respond to a signal it claims to remove. **It is not an applicant-flow study, not
a validated adverse-impact analysis, and passing it is not evidence the system is fair.** The
adverse-impact ratio is reported with the same caveat: where invariance holds it is 1.0 by
arithmetic necessity and adds nothing.

"Disclosure hidden: no" on the last three is expected — an affinity group, a location and a career
break are real extra lines. The system can see a line is there. It cannot see which one.

### Prompt A/B, against a real model

`make prompt-ab` compares two prompt versions on the golden corpus using the local Ollama host.
It **refuses to run against the stub provider** — the stub derives its answer from a hash of the
prompt, so it would produce a difference that is pure noise wearing a result's clothes.

| prompt | competencies lost to the evidence gate | quotes / competency | verified quote rate |
|---|---|---|---|
| v1 | 0.186 | 0.34 | 1.000 |
| v2 (worked example added) | **0.000** | 0.58 | 1.000 |

Three runs each on `qwen3:8b`; the per-run ranges are disjoint (v1 `0.146–0.206`, v2 `0.000`).
The verified-quote rate saturates at 1.000 for both — neither prompt makes the model cite quotes
it cannot support. **The difference is recall, not precision:** v1 left competencies with no
citable evidence at all, and those score zero.

Ten pairs, three runs, one model. v2's `0.000` means it did not fail in thirty scored pairs, not
that it never fails. ([ADR-0021](docs/adr/0021-prompt-ab-against-a-real-model.md))

There is no accuracy percentage anywhere in this project.

---

## Local setup

Everything runs offline and free. No API keys, no accounts, no card.

```bash
git clone <this repo> && cd resume-screener
cp .env.example .env
printf '\nAPP_KEK=%s\nJWT_SECRET=%s\nPOSTGRES_PASSWORD=%s\n' \
  "$(openssl rand -base64 32)" "$(openssl rand -base64 32)" "$(openssl rand -base64 24)" >> .env
make bootstrap && make up && make migrate && make seed
```

Then, with `LLM_PROVIDER=ollama` (default) or `stub` (offline, deterministic):

```bash
make redact-demo    # the before/after image above
make eval           # the golden-set table above
make check          # lint, types, 413 tests, security scanners
```

Requires Docker and [Ollama](https://ollama.com). `ollama pull qwen3:8b` for the local model.

**Deploying it** is documented in [`docs/deployment.md`](docs/deployment.md), including the
part that matters: cloud mode is **genuinely weaker**. Managed Postgres is TCP-only, so the
parse worker cannot keep `network_mode: none` there. `/readyz` reports which mode is running
and whether the strongest claim holds, so nobody has to take the README's word for it
([ADR-0016](docs/adr/0016-cloud-mode-is-explicitly-weaker.md)).

---

## Demo script

1. `make up && make migrate && make seed` — stack healthy, four roles seeded
2. Log in as `recruiter@example.com` (password in `scripts/seed_dev.py`)
3. `POST /resumes` — upload a PDF; watch `worker-parse` redact it with no network
4. `POST /jobs` — create a role with required skills
5. `POST /jobs/{id}/score` — queue every candidate
6. `GET /jobs/{id}/matches` — the ranked list, with every contribution itemised
7. `POST /interviews/{job}/{resume}` — a grounded interview guide

---

## Limitations and non-goals

- **Not a compliant AEDT.** The EU AI Act treats employment screening as high-risk and NYC Local
  Law 144 requires independent bias audits. Neither has been done here.
- **No bias audit.** There is a counterfactual-invariance probe (`make fairness`) and it found
  nine real defects, but it runs on synthetic documents with `n` = 3 per axis. An independent
  audit on real applicant flow is a different thing entirely, and has not been done.
- **Institution redaction is inconsistent.** NER catches `Stanford University` and misses
  `Imaginary Institute`, and an institution is a proxy for background. Measured, recorded in
  ADR-0017, not solved.
- **Webhook delivery does not close the DNS rebinding window.** A URL is
  validated and then resolved again at connect time. The pin-the-IP defence was
  measured and rejected: certificate verification did not follow
  `sni_hostname`, so adopting it would have accepted any certificate. Recorded
  in [ADR-0018](docs/adr/0018-transactional-outbox-and-webhook-egress.md),
  not solved.
- **A stored score is reproducible only up to its prompt *template*.** The rendered prompt carries
  a per-request nonce and freshly generated chunk ids, so re-scoring the same resume can return a
  different number. `matches.prompt_hash` identifies the template, not the render.
- **Synthetic data only.** Every fixture is generated; a CI check enforces the marker.
- **Evaluation is constructed**, so it measures whether the system does what it was designed to
  do — not whether the design is right.
- **No auto-reject path exists.** There is no state transition from `scored` to `rejected`
  anywhere in the code. The absence is the control.

---

## What I learned

Twenty-one [ADRs](docs/adr/) record the decisions. Six where I was wrong, and the measurement that
showed it:

**I claimed the deterministic score was "mathematically immune to injection." It wasn't.**
Instruction injection and keyword stuffing are different attacks. Writing "Kubernetes" into a
resume makes a keyword extractor find Kubernetes with no model involved — the injected resume
scored **1.00 against the honest resume's 0.67** on the very term I had advertised as immune.
([ADR-0012](docs/adr/0012-deterministic-half-is-not-injection-proof.md),
[0014](docs/adr/0014-keyword-stuffing-needs-evidence-weighting.md))

**My own blueprint specified something impossible.** I wrote `network_mode: none` for a worker
that polls a Postgres queue, and called it the most important line in the project. It crash-looped
immediately. Rather than downgrade the guarantee I shared Postgres's unix socket, so the container
keeps *zero* network and still reaches its queue. ([ADR-0008](docs/adr/0008-worker-isolation-via-unix-socket.md))

**The test that guaranteed no endpoint escapes authorization was checking an empty list.** FastAPI
wraps included routers, so scanning `app.routes` found zero routes — the safety net passed
vacuously. It now recurses, and a guard test pins a minimum route count.

**Lexical search returned nothing at all, in production, for months of commits.**
`websearch_to_tsquery` ANDs bare terms, so passing a whole job description demanded a chunk
contain every word. Unit tests missed it by querying single terms. Building the eval harness found
it in one run. ([ADR-0015](docs/adr/0015-retrieval-measured.md))

**Over-redaction is as dangerous as under-redaction, and quieter.** The redactor ate `Redis` and
`(2021-2026)` — a skill and an employment date, both direct inputs to the score. A screener that
redacts a skill still returns a number, just a worse one, for a reason nobody can see.
([ADR-0009](docs/adr/0009-redaction-must-not-eat-the-signal.md))

**A candidate's name changed how their phone number was redacted.** For some names spaCy returned
a spurious five-character span overlapping the phone match, and my merge step — whose docstring
said "longest span wins" while the code did nothing of the sort — discarded the fifteen-character
match in favour of the fragment. The digits went either way, so no PII escaped. What escaped was
*invariance*: the redacted text is what gets embedded and put in the prompt, so two identical
resumes were scored differently because of who they belonged to. Eight more defects came out of
the same probe, including a protected-term list that deleted `man-in-the-middle`, `cache miss` and
`Single Sign-On` from security resumes, and three gender recognizers that could never fire at all.
([ADR-0017](docs/adr/0017-redaction-was-not-name-invariant.md))

The pattern across all six: **my test corpora tested the attacks I had already thought of. The
assembled pipeline tested the ones I hadn't.**

And one that repeated. ADR-0015 recorded that a measurement harness needs its own determinism test
before its numbers mean anything. I wrote the next harness and made the same mistake — it reported
six of seven axes as "differing" until I added a control of byte-identical documents and found
they differed just as much. **Knowing a lesson and applying it are separate skills.**

---

## Stack

FastAPI · PostgreSQL + pgvector · Next.js · Ollama · fastembed (ONNX) · Presidio + spaCy ·
Tesseract · Docker Compose. Postgres doubles as the job queue via `FOR UPDATE SKIP LOCKED`
([ADR-0001](docs/adr/0001-monorepo-and-postgres-as-queue.md)) — no Redis, and job, result and
audit rows commit in one transaction.

$0/month. Every dependency is open source or a free tier, and every hidden cost is flagged in
[the blueprint](docs/BLUEPRINT.md).

## License

Apache-2.0. See [ADR-0002](docs/adr/0002-pdf-library-and-licence.md) for why the PDF library
choice is a licence decision.

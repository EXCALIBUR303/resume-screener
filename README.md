# Secure Multi-Modal AI Resume Screener

A resume screening system that **redacts before it reasons**, scores with evidence it can
prove, and survives a resume that tries to talk to the model.

[![license](https://img.shields.io/badge/license-Apache--2.0-2E6B5B)](LICENSE)

> **Status.** 413 tests pass and every scanner is clean **locally**. These are static
> badges, not CI status — the workflows in `.github/workflows/` have never executed,
> because this repository has no remote yet. Replace this line with real badges once
> it is pushed and a run goes green.

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
Bengaluru, India | Female, married |         LOCATION_1, LOCATION_2 | GENDER_1,
  D.O.B: 12/04/1997 | PAN ABCDE1234F           MARITAL_1 | DOB_1 | ORG_1

WORK EXPERIENCE                              WORK EXPERIENCE
Senior Backend Engineer,                     Senior Backend Engineer,
  Invented Systems Ltd (2021-2026)             ORG_2 (2021-2026)
Priya designed payment services in           PERSON_1 designed payment services in
  Python on PostgreSQL at 12k req/s            Python on PostgreSQL at 12k req/s

TECHNICAL SKILLS                             TECHNICAL SKILLS
Python, PostgreSQL, Redis, Docker,           Python, PostgreSQL, Redis, Docker,
  Kubernetes, REST APIs, pytest                Kubernetes, REST APIs, pytest
```

17 identifiers removed. **Every skill, employment date and achievement intact.** Redaction runs
inside a worker with no network access, before anything is embedded, prompted, indexed or logged.

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
| **Interview copilot** | Questions grounded in verified gaps, with anchored rubrics; ungrounded or unlawful questions are rejected |
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

---

## Evaluation

Golden set v1: 50 synthetic resumes × 8 job descriptions = 400 pairs.

| retriever | nDCG@10 | P@5 | Recall@20 | MRR |
|---|---|---|---|---|
| vector | 0.812 | 0.550 | 0.975 | 0.792 |
| lexical | **0.915** | **0.675** | 1.000 | 0.917 |
| hybrid | 0.907 | 0.650 | 1.000 | **1.000** |

**Read this honestly.** Labels are *derived from construction*, not human judgment — see
[`evals/README.md`](evals/README.md) for what that buys and costs. Hybrid retrieval **does not**
beat lexical on nDCG@10 here; the corpus writes skill names literally and so favours exact
matching. Hybrid does rank a relevant candidate first for **every** job description, which
neither retriever alone manages. That is the only claim this evidence supports, and it is the
only one made.

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
- **No bias audit.** Protected attributes are removed before the model sees them, which reduces
  obvious exposure and is *not* the same as being fair.
- **Synthetic data only.** Every fixture is generated; a CI check enforces the marker.
- **Evaluation is constructed**, so it measures whether the system does what it was designed to
  do — not whether the design is right.
- **No auto-reject path exists.** There is no state transition from `scored` to `rejected`
  anywhere in the code. The absence is the control.

---

## What I learned

Fifteen [ADRs](docs/adr/) record the decisions. Five where I was wrong, and the measurement that
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

The pattern across all five: **my test corpora tested the attacks I had already thought of. The
assembled pipeline tested the ones I hadn't.**

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

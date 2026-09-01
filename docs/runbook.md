# Runbook

Every procedure here has been executed at least once, on 2026-09-01, against a
running stack. Where a step has never been run in anger that is stated, because
an untested procedure is a guess with formatting.

## Contents

- [Observability](#observability)
- [Dead-letter queue](#dead-letter-queue)
- [Queue not draining](#queue-not-draining)
- [Redaction stopped](#redaction-stopped)
- [Model unavailable](#model-unavailable)
- [Evidence zeroed](#evidence-zeroed)
- [Backup and restore](#backup-and-restore)
- [Rotating the KEK](#rotating-the-kek)
- [Rolling back a deploy](#rolling-back-a-deploy)

---

## Observability

```bash
docker compose --profile observability up -d
```

| | |
|---|---|
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3001 (`admin` / `$GRAFANA_PASSWORD`) |
| Raw metrics | `curl -s localhost:8000/metrics` |

**`/metrics` is unauthenticated**, because that is how Prometheus scrapes. It
exposes operational shape — queue depth, rejection reasons, throughput — so it
must stay on an internal network. Caddy does not proxy it. Set
`METRICS_ENABLED=false` to remove it entirely.

Traces are opt-in (`OTEL_ENABLED=true`) and carry W3C context through the job
payload, so one trace spans API → queue → worker rather than producing four
unrelated single-span traces.

---

## Dead-letter queue

**Alert:** `DeadLetterQueueNonEmpty` — a job was abandoned after exhausting its
retries or hitting a terminal error. Nobody finds these by accident.

```bash
# What died and why
docker compose exec -T db psql -U screener -d screener -c \
  "SELECT id, job_type, attempts, error_class, left(last_error, 80)
     FROM job_queue WHERE status = 'dead' ORDER BY finished_at DESC LIMIT 20;"
```

Read `error_class` first:

- **`terminal`** — retrying will not help. An unsupported MIME type, a document
  with no extractable text, a schema failure that survived its repair attempt.
  Usually the right answer is to leave it dead and fix the input.
- **`retryable`** — it exhausted `max_attempts`. Something was down. Replay once
  the cause is fixed:

```bash
curl -X POST localhost:8000/admin/dlq/<job_id>/replay -H "authorization: Bearer $TOKEN"
```

Replay requires `dlq:manage` (org_admin or owner) and writes an audit event.
Operator actions that resurrect failed work should be attributable afterwards.

---

## Queue not draining

**Alert:** `QueueNotDraining` — oldest pending job over 15 minutes.

Depth flat while age rises means workers are **stuck**, not busy. Different
problem, different fix.

```bash
docker compose ps                    # is a worker missing or restarting?
docker compose logs worker-parse --tail=50
docker compose logs worker-ai --tail=50
```

Jobs held by a dead worker are reclaimed automatically once
`WORKER_LEASE_TIMEOUT_SECONDS` (300) passes — the sweeper returns them to
`pending`. If a worker is crash-looping, the queue is a symptom and the logs are
the cause.

---

## Redaction stopped

**Alert:** `RedactionProducingNothing`, severity critical.

This is the most dangerous silent failure the system has: uploads keep
succeeding, scores keep appearing, and raw PII is reaching the model.

```bash
docker compose logs worker-parse --tail=100 | grep -i 'redaction'
```

`redaction.ner_unavailable` means the spaCy model failed to load — the
deterministic layers still run, so recall degrades rather than collapsing, but
it must be fixed. The model is baked into the image, so this usually means a bad
build:

```bash
docker compose exec worker-parse python -c "import spacy; spacy.load('en_core_web_sm')"
```

If `REDACTION_ENABLED` is false outside a unit test, that is the bug. Fix it and
**treat every resume processed in that window as exposed**.

---

## Model unavailable

**Alert:** `ModelFailing` — over a quarter of calls failing.

Scoring degrades rather than stopping: the deterministic terms still produce a
score, the result is flagged `degraded`, and the UI says so. No candidate is
dropped because the model was down.

```bash
curl -s localhost:11434/api/tags | head          # is Ollama up?
docker compose logs worker-ai --tail=50 | grep llm
```

The circuit breaker opens after `LLM_CIRCUIT_BREAKER_FAILURES` (5) consecutive
failures and closes itself after 60 seconds. Queued scoring jobs are retried;
they are not lost.

---

## Evidence zeroed

**Alert:** `EvidenceFabricationRising` (informational).

Competencies are being zeroed more often than usual, meaning cited quotes are
not appearing in the source. Two very different causes:

1. **A document is attacking us.** Check whether `injection_suspected` rose at
   the same time. If so the system is working — the attack is being priced in.
2. **The model is degrading.** If injection flags are flat, look at whether the
   model or prompt version changed. Every `matches` row records both, which is
   what makes this answerable:

```sql
SELECT model_id, prompt_version, count(*),
       avg((rubric->>'aggregate_groundedness')::float)
  FROM matches GROUP BY 1, 2 ORDER BY 3 DESC;
```

---

## Backup and restore

**Run monthly.** An untested backup is not a backup.

```bash
AGE_RECIPIENT=age1... ./scripts/backup.sh     # encrypted
./scripts/backup.sh                            # warns loudly if unencrypted
```

Restore goes to a **scratch database** and refuses to overwrite the live one:

```bash
./scripts/restore.sh backups/db-<stamp>.dump
```

**Executed 2026-09-01.** Restored into `screener_restore_test` and verified:
2 organizations, 5 users, 54 resumes, 50 chunks, 4 matches, 27 audit events.
Drop the scratch database afterwards.

⚠️ Encryption is **not** yet exercised: `age` was not configured on the machine
where this was run, so the drill covered the dump and restore path only. Set
`AGE_RECIPIENT` and repeat before relying on encrypted backups.

---

## Rotating the KEK

Envelope encryption is why this is cheap: every blob has its own data key and
only the wrapped keys are re-encrypted. Ciphertext is never rewritten, so
rotating 100 GB costs what rotating 1 MB costs.

```bash
# 1. Dry run FIRST. It proves the old key can unwrap every record.
APP_KEK_OLD="$CURRENT" APP_KEK="$NEW" python scripts/rotate_kek.py

# 2. Apply
APP_KEK_OLD="$CURRENT" APP_KEK="$NEW" python scripts/rotate_kek.py --apply

# 3. Blobs live in a Docker volume, so that pass must run where it is mounted
docker compose exec worker-ai python /app/scripts/rotate_kek.py --apply

# 4. Point the app at the new key, then retire the old one
#    APP_KEK=<new>  APP_KEK_VERSION=<n+1>
docker compose up -d api worker-ai worker-parse
```

**Executed 2026-09-01.** Dry run unwrapped 4 `pii_map` rows with the old key;
`--apply` re-wrapped all 4 at version 2; the new key decrypts them; the retired
key is correctly rejected; the API served a ranked list afterwards.

**Do not delete the old key until step 4 is verified.** Both keys must exist for
the duration: the old to unwrap, the new to re-wrap. That is the whole risk in
this procedure.

---

## Rolling back a deploy

Images are tagged by commit SHA and referenced by digest, so rollback is
redeploying the previous digest.

```bash
docker compose down
git checkout <previous-sha>
docker compose up -d --build
```

Migrations are backward-compatible for one version (expand/contract) and every
one has a tested `downgrade`. If a migration must be reversed:

```bash
make downgrade      # one step
```

⚠️ **Never run in anger.** The up/down/up cycle is exercised by CI on every push,
but a production rollback has never been performed — there is no production.

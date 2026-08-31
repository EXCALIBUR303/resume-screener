# ADR-0001 — Monorepo, and Postgres as the job queue

**Status:** accepted · **Date:** 2026-08-31

## Context

Solo developer, part-time, zero budget. Two structural choices set the ceiling on how much
infrastructure work the project generates later: how the code is laid out, and what carries
async work between the API and the workers.

## Decision 1 — monorepo

`apps/{api,web,worker}` + `packages/contracts` in one repository, one CI pipeline, one
version history.

**Why:** the contract between the API and the web app is the thing most likely to break
silently. In a monorepo, JSON Schema in `packages/contracts` generates both the Pydantic
models and the TypeScript types, so a breaking backend change fails `tsc` in the same CI run
that introduced it. Split repos would need a package registry and version negotiation — real
work that buys nothing at this size.

**Cost accepted:** CI runs more than strictly needed on a change touching one app. Mitigated
with path filters if runs ever exceed five minutes.

## Decision 2 — Postgres as the job queue, not Redis

A `job_queue` table consumed with `SELECT … FOR UPDATE SKIP LOCKED`.

**Why:**

1. **Transactional integrity.** The job row, the result row, and the audit event commit in a
   single transaction. With Redis, "worker finished but crashed before writing the result" is
   a real state that needs reconciliation logic. Here it cannot happen.
2. **Idempotency is a `UNIQUE` constraint**, not application code. Rule D-6 becomes a schema
   guarantee.
3. **One less service** to run locally, secure, back up, and pay for. Every free host offers
   Postgres; free Redis tiers are scarcer and smaller.
4. **The DLQ is a `WHERE` clause.** Inspecting and replaying dead jobs needs no new tooling.

**Cost accepted:** throughput ceiling is far lower than Redis — roughly hundreds of jobs/sec
rather than tens of thousands. For a screening tool processing resumes at human pace this is
several orders of magnitude of headroom. Polling adds latency (500 ms default) and some
write load from lease updates.

**Upgrade path:** the worker consumes a `Queue` protocol. Swapping in Redis + RQ later means
one new implementation, not a rewrite. Revisit if sustained throughput exceeds ~100 jobs/sec
or poll latency becomes user-visible.

## Consequences

- `VACUUM` pressure on `job_queue` needs watching; partial indexes on `status='pending'`.
- A lease sweeper is required for workers that die mid-job (at-least-once delivery), which is
  safe precisely because of the idempotency keys in decision 2.

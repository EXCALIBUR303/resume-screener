# ADR-0006 — Container Postgres uses host port 5433

**Status:** accepted · **Date:** 2026-08-31

## Context

A native Homebrew `postgresql@17` service is running on this machine, bound to
`127.0.0.1:5432` and `[::1]:5432`. Docker also published the container's 5432.

The native service wins for host-originated connections. Anything run from the host —
`alembic upgrade`, the seed script, `psql` — silently connected to the **wrong database**.
The failure looked like `role "screener" does not exist`, which reads as a container problem
and is not one.

## Decision

Publish the container as `5433:5432`. Inside the compose network the API still reaches it as
`db:5432`; only host-side access moves.

Rejected: stopping the Homebrew service. It belongs to other work on this machine and this
project has no business turning it off.

## Consequences

- Host-side commands need `POSTGRES_PORT=5433`. `make migrate` and `make seed` set it.
- CI is unaffected — GitHub runners have no native Postgres on 5432.
- A second symptom of the same class as ADR-0005: the development machine is not a clean
  room, and a project that only works on a clean machine is not finished. Both are recorded
  so the next confusing "it works in CI" report has somewhere to start.

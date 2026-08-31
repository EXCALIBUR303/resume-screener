# ADR-0008 — `worker-parse` reaches Postgres over a unix socket

**Status:** accepted · **Date:** 2026-08-31 · **Amends:** the M3 spec in `BLUEPRINT.md`

## The flaw in my own design

The blueprint specified `worker-parse` with `network_mode: none`, and called it "the single
most important line in the whole project". At implementation time it turned out to be
**impossible as written**: `network_mode: none` removes every interface except loopback, and
the worker polls a Postgres queue over TCP. The container started, crashed on its first
`claim()`, and restart-looped.

The spec asserted a property without checking it was achievable. Writing the design down did
not make it correct; building it did.

## Options considered

| Option | Isolation | Cost |
|---|---|---|
| `internal: true` network | No internet route, but full container-to-container TCP | Weakest — a compromised parser still reaches the API and every other service |
| Supervisor + sandboxed subprocess | Strong | A second process model; significant complexity |
| **Unix socket to Postgres** | **True `network_mode: none`** | Share Postgres's socket directory as a volume |

## Decision

Postgres's socket directory (`/var/run/postgresql`) is a named volume mounted into both `db`
and `worker-parse`. The worker sets `POSTGRES_SOCKET_DIR`, and `Settings.dsn` builds
`postgresql+psycopg://user:pass@/db?host=/var/run/postgresql`.

The worker keeps `network_mode: none` — the strongest option — and still reaches its queue.

## Verified, not asserted

Inside the running container:

```
interfaces:   lo, tunl0, gre0, …   (no routable interface)
1.1.1.1:53    blocked (OSError)
8.8.8.8:443   blocked (OSError)
db:5432       blocked (gaierror — DNS cannot even resolve it)
api:8000      blocked (gaierror)
unix socket   job_queue reachable, count returned
touch /probe  Read-only file system
id            uid=10002(worker) — non-root
```

A parser RCE lands somewhere with no network stack, a read-only root filesystem, no
capabilities, and a 1 GB address-space cap. The only reachable thing is a Postgres socket
belonging to a role with no rights over `audit_events`.

## Consequences

- `db` must expose a unix socket. Every self-hosted Postgres does; a **cloud** database
  (Neon, Supabase) is TCP-only. In cloud mode the worker must fall back to an
  `internal: true` network, which is strictly weaker. The README has to say so plainly rather
  than implying the local guarantee travels.
- `Settings.dsn` now has two shapes, both covered by tests.
- The blueprint's M3 section is amended by this ADR rather than quietly edited. The mistake
  is more instructive than a clean spec.

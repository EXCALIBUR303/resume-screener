# ADR-0016 — Cloud mode is a weaker deployment, declared as one

**Status:** accepted · **Date:** 2026-09-01 · **Extends:** ADR-0008

## The problem

ADR-0008 kept the strongest guarantee in the project: the parse worker — the
only process that touches attacker-controlled bytes — runs with
`network_mode: none` and reaches Postgres over a shared unix socket. It has no
routable interface at all.

**No managed Postgres offers a unix socket.** Neon, Supabase and every other
free-tier provider are TCP-only. So in a cloud deployment the parse worker must
sit on a network, and a parser RCE has somewhere to go. The guarantee does not
travel.

## Options

| Option | Verdict |
|---|---|
| Ship cloud mode without mentioning it | Rejected. The README makes a specific security claim; letting it silently become false in the deployed instance is the exact failure this project exists to avoid. |
| Refuse to support cloud deployment | Rejected. A portfolio project a recruiter cannot click is worth less, and "we could not do it safely" is only honest if the alternative was actually unsafe. It is weaker, not unsafe. |
| **Make the downgrade an explicit, reported mode** | Chosen. |

## Decision

`DEPLOYMENT_MODE` is `local` or `cloud`, set deliberately, and `/readyz` reports
both it and whether the strongest claim holds:

```json
{"deployment_mode": "cloud", "parse_worker_network_isolated": false}
```

An operator looking at a deployed instance can see which guarantees apply there
rather than reading the README and assuming. `docs/deployment.md` states the
difference in its first table, before the setup instructions.

## A bug this immediately caught

The first implementation derived the flag from `deployment_mode == "local" and
postgres_socket_dir`. A correctly-configured **local** stack then reported
`false` — because that is the *API's* socket setting, and the API does not use
the socket. A process cannot observe another container's network from its own
environment.

The property now reports the declared mode only, and says so in its docstring.
What actually enforces the guarantee is the compose file; what verifies it is
`test_worker_parse_declares_no_network`, which reads that file, plus the live
probe recorded in ADR-0008. **The flag is a label, not evidence, and the code
says which it is.**

## Consequences

- Cloud mode also loses ClamAV (RAM), gains cold starts (autosuspend), and sends
  redacted text to a third-party model whose free tier may train on it — hence
  synthetic data only.
- `S3BlobStore` exists so ephemeral container disks are survivable. It applies
  the same envelope encryption as the local store, so the provider holds
  ciphertext: a misconfigured bucket policy leaks encrypted blobs, not resumes.
  A test asserts the bucket never contains plaintext.
- No cloud deployment has been performed. `docs/deployment.md` says so plainly
  rather than reading as though it had.

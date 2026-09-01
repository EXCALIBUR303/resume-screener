# ADR-0024 — Cloud storage was never wired up, and a Dockerfile for the web frontend

**Status:** accepted · **Date:** 2026-09-01 · **Found while writing the cloud deployment walkthrough**

## What was there and never connected

`S3BlobStore` has existed since M12 — tested, encrypted the same way as the
local store, content-addressed the same way. `Settings.storage_backend:
Literal["local", "s3"]` has existed just as long. Neither `worker.py` nor
`routers/resumes.py` ever read the setting: both constructed `BlobStore`
directly, unconditionally.

```python
store = BlobStore(settings.storage_local_path, kek=kek, kek_version=settings.app_kek_version)
```

Setting `STORAGE_BACKEND=s3` in a deployment changed nothing. Every resume
would have been written to the container's local disk — ephemeral on every
host this project's cloud docs recommend — and `S3BlobStore` would have sat in
the codebase, fully built, reachable by nothing.

This was not caught by a report. It surfaced while writing the cloud
deployment walkthrough and actually reading what `deploy.yml` and the worker
do with the S3 secrets the docs ask for, rather than assuming the plumbing
matched the documentation.

## The fix

One factory, `build_store()`, is now the only place `storage_backend` is read.
Both call sites ask it for "the store" instead of constructing one:

```python
store = build_store(settings, kek=kek, kek_version=settings.app_kek_version)
```

A guard test greps both files for a direct `BlobStore(` construction and fails
if either bypasses the factory — verified by reverting each call site by hand
and confirming the test fails before trusting it, the same discipline ADR-0017
and ADR-0020's guards needed after two of mine turned out not to fire.

`S3BlobStore` also gained explicit `access_key_id`/`secret_access_key`
parameters. It previously relied on boto3's own ambient `AWS_*` environment
variable chain, silently, while every document in this project promises
`S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY`. Two names working when only one is
documented is exactly how a credential ends up unset in production — it was
named the way the docs said, and the code was listening for something else.
`Settings` gained the four fields (`s3_bucket`, `s3_endpoint_url`,
`s3_access_key_id`, `s3_secret_access_key`) it never had.

## A worker with no reason to hold a port, on hosts that require one

Most single-container hosts (Hugging Face Spaces among them) treat "nothing is
listening" as indistinguishable from "the process crashed." This worker is a
pure background consumer — nothing calls it over HTTP — so it had no listener
and no way to pass that kind of health check.

`health.py` adds one, and it is not a web server pretending to be one: it
answers exactly "is the poll loop still ticking," by comparing the current time
to a heartbeat the loop updates every iteration. A worker wedged inside a
hung network call stops updating the heartbeat and the listener starts
returning 503 after `STALE_AFTER_SECONDS` (180s — generous next to a single
poll, tight next to a genuinely stuck job), which is what makes it a liveness
check rather than a process-existence check. Opt-in via `HEALTH_PORT`: unset
means no listener at all, which keeps local and compose behaviour unchanged —
opening a port nobody asked for is not something a local dev run should do
by default.

Tested against real TCP sockets, not by calling the handler function directly
— including a malformed, non-HTTP request, because the container's only open
port must survive garbage without going down, or it looks identical to a
crash from the outside.

## `Dockerfile.web`

`apps/web` builds with `output: "standalone"` (a Node server), which does not
fit Cloudflare Pages' zero-config Next.js support — that wants a static export
or their own adapter. It fits a container directly, which this Dockerfile
builds: multi-stage, the standalone trace as the runtime (no `npm install` at
runtime — the build stage already traced the minimal `node_modules` the server
needs), non-root.

`NEXT_PUBLIC_API_BASE_URL` is a build argument, not a runtime environment
variable — Next.js inlines every `NEXT_PUBLIC_*` value into the client bundle
at `next build` time, so setting it on the running container would do nothing
to the JavaScript already shipped to a browser. Confirmed rather than assumed:
built the image with a placeholder API URL and grepped the compiled bundle for
it before trusting the Dockerfile.

Built and run locally before this was written up as usable: `docker build`
succeeds, the container serves `HTTP 200` on its port, and the build-arg URL
is verifiably present in the compiled JavaScript.

## What this does not establish

The three-Space (or three-host) architecture this unblocks — `api`,
`worker-parse`, `worker-ai`, each reachable and correctly configured, sharing
one Neon database — has not been deployed anywhere. This ADR is the code being
made correct and locally verified; the walkthrough for actually standing it up
is separate, and its own status note will say plainly whether it has been run.

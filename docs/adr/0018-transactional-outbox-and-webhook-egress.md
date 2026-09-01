# ADR-0018 — Transactional outbox, and treating webhook egress as hostile

**Status:** accepted · **Date:** 2026-09-01 · **Builds on** ADR-0001

## Why an outbox and not an HTTP call

The obvious implementation is a `POST` at the end of `handle_score_job`. It is
wrong, and the reason is visible in the worker loop:

```python
await handle_score_job(session, payload, gateway=gateway, prompt=prompt)
await complete(session, job)
await session.commit()          # <- the score becomes real HERE
```

An HTTP call inside the handler fires **before** that commit. If the commit
fails — a constraint violation, a lost connection, a lease that expired and let
another worker take the job — the score never existed and the customer has
already been told it did. No retry policy repairs that: the event is not
delayed, it is false.

Writing a row into `outbox_events` in the same session makes the event part of
the same commit. If the score is not durable, neither is the notification.

The cost is real and is not hidden: delivery becomes **at-least-once** and
asynchronous. A relay can post successfully and die before committing the row,
and the event is delivered again. Receivers deduplicate on `event_key`, which
is stable across redeliveries and travels in both the body and a header.

## Webhook egress is the largest attack surface in the system

A webhook URL is typed in by a tenant and fetched by our infrastructure. That is
server-side request forgery by definition, and the interesting targets are not
on the internet:

```
http://169.254.169.254/latest/meta-data/iam/security-credentials/
http://metadata.google.internal/computeMetadata/v1/
http://localhost:5432
```

Four decisions follow.

**HTTPS only, and validate before storing.** A stored bad URL survives a
restart; checking at delivery time leaves a queue of requests aimed at the
metadata service.

**Judge the addresses, not the hostname.** `localtest.me` and a thousand other
public names resolve to 127.0.0.1. Every address a name resolves to must be
global unicast — *every* one, because a name returning one public and one
private address would otherwise pass the check and connect to the private one
on a retry.

**No redirects.** Following a 302 would let a validated public URL bounce the
request to 169.254.169.254 — every address check undone by one response header.
Disabled at the call site and again on the client, so a future call site cannot
re-enable it by forgetting an argument.

**Its own process, with its own key.** `worker-parse` has no network at all
(ADR-0008). `worker-ai` reaches one destination we control. This process
reaches URLs a tenant chose, anywhere on the public internet, which is a
different kind of privilege — and putting it in the process that runs the
scoring pipeline would hand an attacker who reached that pipeline a way out.
`worker-webhook` is a separate service with no blobs volume, and it derives its
key with `purpose="webhook"` so the key it holds decrypts endpoint signing
secrets and cannot open a candidate's PII map.

That last point needs stating precisely: **anyone holding `APP_KEK` can derive
both keys.** This is domain separation, not isolation from an attacker who has
the root secret. What it buys is that a compromise confined to the relay — the
process that talks to the internet — does not hand over candidate data.

## The DNS rebinding gap, which is not closed

Validation and connection are separate resolutions. Between them, DNS can
change its answer: public for the check, `169.254.169.254` for the connection.

The standard defence is to connect to the address that was validated and carry
the original hostname for TLS. The mechanism available here is rewriting the URL
to the IP and passing `extensions={"sni_hostname": host}`, so I measured it
before trusting it:

```
sni=example.com                        -> 200
sni=definitely-not-this-host.invalid   -> 200
```

**Certificate verification did not follow the extension.** A deliberately wrong
SNI hostname completed the handshake. Adopting the pin would have traded a
narrow timing window for "any certificate is accepted", which is a worse control
than none, so it was not adopted and the docstring that claimed it was corrected.

What remains: an attacker needs DNS control over a name they registered, a TTL
short enough to change the answer between two resolutions milliseconds apart,
and their reward is that a signed JSON document of identifiers reaches an
address of their choosing. Narrow, real, and recorded here rather than described
as solved.

## Payloads carry no PII, enforced by an allowlist

An audit row stays in our database. A webhook payload leaves our network for a
URL a tenant chose, so the rule is the audit log's rule applied harder:
identifiers and non-identifying metadata only.

`assert_no_pii` enforces an **allowlist**, not a denylist. A denylist has to
anticipate every field name that might one day hold a person's name, and it
only has to be wrong once. A second check rejects any string over 200
characters, because an allowlisted key could still be handed a paragraph of
resume prose in good faith.

## Signatures

`v1=HMAC-SHA256(secret, "<timestamp>.<body>")`, with the timestamp signed
rather than merely sent beside the body. Signing the body alone produces a
token valid forever: capture one request and replay it indefinitely. The
receiver's tolerance window only means something if the timestamp is inside the
MAC.

The body is canonical JSON — sorted keys, fixed separators — because the
receiver recomputes the MAC over the bytes it received, and re-serialising the
same document with different whitespace would fail verification on an unmodified
payload.

`verify()` ships alongside `sign()` and the tests exercise it, so the documented
verification procedure is executable rather than a snippet in a README that has
never run.

## A dev-only flag that disables a security control

`WEBHOOK_ALLOW_PRIVATE_DESTINATIONS` lets a webhook point at a container on the
compose network. It turns off the address check, so:

- the process **refuses to start** with it set outside `APP_ENV=dev`, the same
  guard that refuses placeholder secrets;
- `/readyz` reports `webhook_ssrf_check_enabled`, because a disabled control
  should be visible on the instance rather than discoverable only by reading
  someone's `.env`.

It does **not** relax the https-only rule. Demonstrating the relay against a
local plaintext receiver would have needed a second bypass, and a demo is not
worth that. The delivery loop is proven instead by an integration test that runs
claim → decrypt → sign → deliver → settle against a real database with a mock
transport, which covers every step that touches state.

## Consequences

- One new process to run. `docker compose up` starts it; without it, events
  accumulate as `pending` rather than being lost.
- Delivery is at-least-once. Documented, and `event_key` is the answer.
- An endpoint that fails 20 times in a row is disabled rather than retried
  forever. A tenant who deleted their receiver should not cost a request a
  minute in perpetuity.
- Events that exhaust their attempts become `dead`, not deleted. An event
  nobody could deliver is evidence.

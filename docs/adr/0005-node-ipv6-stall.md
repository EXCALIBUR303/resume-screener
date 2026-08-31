# ADR-0005 — Node hangs on hosts with AAAA records (no IPv6 connectivity)

**Status:** resolved · **Date:** 2026-08-31 · **Supersedes:** the earlier "VPN" diagnosis

## Symptom

`pnpm install` and `npm install` in `apps/web` hang, then fail with `ETIMEDOUT` /
`UND_ERR_CONNECT_TIMEOUT`. The Python side installs fine via `uv`.

## Wrong turn, recorded on purpose

The first diagnosis was "a VPN is routing Cloudflare over a tunnel with a broken MTU",
based on six `utun` interfaces and a running `com.nordvpn.macos.helper`. **That was wrong.**
`utun` interfaces are normal on macOS, all six held only link-local addresses, and
`route -n get default` showed traffic going out `en0` to the LAN gateway — no tunnel
involved. Inferring a cause from a suggestive-looking symptom, without checking the routing
table, cost about twenty minutes.

## Actual cause

| Evidence | Reading |
|---|---|
| `ifconfig en0` has no non-link-local `inet6` | **The machine has no global IPv6 address** |
| `curl -6 registry.npmjs.org` fails in 0.03 s | No IPv6 route at all |
| `registry.npmjs.org`, `example.com` have AAAA records | Node tries IPv6 first |
| `github.com` has **no** AAAA record | Which is exactly why it was the only host that worked |
| `node fetch` direct to `104.16.5.34` → `ERR_TLS_CERT_ALTNAME_INVALID` in 2.1 s | TCP **and** TLS succeeded; only the cert name mismatched. The network path was never the problem. |

Node's `fetch` (undici) attempts the AAAA address and stalls on a network with no IPv6
route. `curl` falls back to IPv4 quickly; Node does not. The apparent "Cloudflare is
blocked" pattern was coincidence — Cloudflare publishes AAAA records and GitHub did not.

## Fix

```
NODE_OPTIONS="--no-network-family-autoselection --dns-result-order=ipv4first"
```

`npm install` then completed in **15 seconds**. `--dns-result-order=ipv4first` alone is not
enough; undici's happy-eyeballs still probes IPv6 without
`--no-network-family-autoselection`.

**pnpm ignores `NODE_OPTIONS`** — its launcher is a compiled binary. Use `npm` on this
machine, or run pnpm under an explicit `node` invocation. `make bootstrap` sets the variable
and falls back to npm.

## Consequences

- Every `make` target that touches Node exports `NODE_OPTIONS`.
- For interactive shells, add the same export to `~/.zshrc`.
- Machine-wide alternative (needs admin, affects everything):
  `sudo networksetup -setv6off Wi-Fi`.
- CI is unaffected: GitHub runners resolve and route IPv6 correctly.
- Second entry in the "the dev machine is not a clean room" file, with ADR-0006.

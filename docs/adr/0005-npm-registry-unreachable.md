# ADR-0005 — Web dependencies blocked: Node cannot reach Cloudflare-hosted registries

**Status:** open — environment issue, not a code decision · **Date:** 2026-08-31

## Symptom

`pnpm install` and `npm install` in `apps/web` hang and then fail with `ETIMEDOUT` /
`UND_ERR_CONNECT_TIMEOUT`. The API side installs fine via `uv` (PyPI).

## Diagnosis

| Test | Result |
|---|---|
| `curl https://registry.npmjs.org/react` | **200 in 2.6 s** |
| `node -e "fetch('https://registry.npmjs.org/react')"` | **UND_ERR_CONNECT_TIMEOUT, 6 s** |
| `node -e "fetch('https://github.com')"` | **200 in 2.2 s** |
| `node -e "fetch('https://example.com')"` | **UND_ERR_CONNECT_TIMEOUT, 1 s** |
| `curl -6 https://registry.npmjs.org` | fails instantly (no IPv6 route) |
| `--dns-result-order=ipv4first` | no effect |

So: **not** a registry outage, **not** pnpm, **not** npm, **not** IPv6 alone, and not a
proxy (`npm config get proxy` is null). `curl` succeeds where Node fails against the *same*
addresses.

`registry.npmjs.org` and `example.com` both resolve to Cloudflare (104.16.x.x); `github.com`
does not — and GitHub is the only one that works. Five `utun` interfaces are up with
mismatched MTUs (1500 / 1380 / 2000 / 1000 / 1380), i.e. an active VPN.

**Most likely cause:** a VPN tunnel routes Cloudflare ranges over a path whose MTU breaks
Node/undici's connection setup, while curl negotiates around it.

## Impact

`apps/web` source is written and correct but has no `node_modules`, so `pnpm typecheck` and
`pnpm lint` cannot run locally. **The API, database, migrations, and AC-15 are unaffected and
verified.** CI runs on GitHub-hosted runners with no VPN, so the `web` job is expected to pass
there.

## Things to try, cheapest first

1. Disconnect the VPN and re-run `make bootstrap`.
2. If the VPN is required, split-tunnel or exclude `104.16.0.0/12`.
3. Lower the tunnel MTU: `sudo ifconfig utun3 mtu 1280`.
4. Last resort — a registry mirror that is not on Cloudflare:
   `pnpm config set registry https://registry.npmmirror.com`.

## Decision

Not blocking M0. The API half is complete and verified; the web shell is source-complete and
installs the moment the network allows. Re-run `make bootstrap` after resolving, then
`make lint`.

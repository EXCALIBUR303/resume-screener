# Deploying

The **local stack is canonical**. The cloud deployment is a shop window, and it
is genuinely weaker — that is stated here rather than discovered later.

## What changes in the cloud, and what it costs

| | local | cloud |
|---|---|---|
| Parse worker network | **`network_mode: none`** — no interface at all, Postgres over a unix socket | **On a network.** Managed Postgres is TCP-only |
| Blob storage | Encrypted files on disk | Encrypted objects in S3-compatible storage |
| Model | Ollama, local, private | Free-tier endpoint. ⚠️ **Free tiers commonly reserve the right to train on inputs** |
| Data | Whatever you upload | **Synthetic only** |
| Cold start | None | Up to ~30 s (Neon autosuspend, Spaces sleep) |
| ClamAV | Optional profile | Off — not enough RAM on free tiers |

The first row is the important one. [ADR-0008](adr/0008-worker-isolation-via-unix-socket.md)
describes how the parse worker keeps zero network locally by reaching Postgres
over a shared unix socket. **No managed Postgres offers that**, so in cloud mode
the worker sits on an internal network and a parser RCE has somewhere to go.

This is why `DEPLOYMENT_MODE` is an explicit setting rather than something
inferred: `/readyz` reports which mode is running and whether the strongest claim
holds, so nobody reads the README and assumes.

```bash
curl -s https://<your-api>/readyz
{"status":"ok", "deployment_mode":"cloud", "parse_worker_network_isolated":false}
```

## What you need to create

These need your accounts — I cannot create them for you. All are free tiers.

| # | Service | For | Notes |
|---|---|---|---|
| 1 | [Neon](https://neon.tech) | Postgres + pgvector | ~0.5 GB free, no card. Run `CREATE EXTENSION vector;` once |
| 2 | [Cloudflare R2](https://developers.cloudflare.com/r2/) *or* [Supabase Storage](https://supabase.com/storage) | Blobs | ⚠️ R2 has historically wanted a card on file; Supabase does not |
| 3 | [Hugging Face Spaces](https://huggingface.co/spaces) (Docker SDK) | API + worker | Free CPU tier, sleeps when idle |
| 4 | [Cloudflare Pages](https://pages.cloudflare.com) | Web | Commercial use permitted, unlike Vercel Hobby |
| 5 | [Groq](https://console.groq.com) *or* [Google AI Studio](https://aistudio.google.com) | Model | Rate-limited. Synthetic data only |

Then add these as GitHub Actions secrets:

```
NEON_DATABASE_URL      postgresql+psycopg://...
S3_ENDPOINT_URL        https://<account>.r2.cloudflarestorage.com
S3_BUCKET              screener-blobs
S3_ACCESS_KEY_ID       ...
S3_SECRET_ACCESS_KEY   ...
LLM_BASE_URL           https://api.groq.com/openai/v1
LLM_API_KEY            ...
APP_KEK                openssl rand -base64 32   ← generate fresh, never reuse local
JWT_SECRET             openssl rand -base64 32   ← generate fresh
```

`APP_KEK` must be **new**. Reusing the development key would mean a laptop
compromise decrypts production, and rotating it later is a procedure
([runbook](runbook.md#rotating-the-kek)) rather than an edit.

## Deploying

```bash
gh workflow run deploy.yml
```

The workflow refuses to run until every secret above is set — a half-configured
deploy that boots and then fails on first upload is worse than one that will not
start.

## Seeding the demo

```bash
DEPLOYMENT_MODE=cloud DATABASE_URL=$NEON_DATABASE_URL \
  python scripts/seed_dev.py
python scripts/gen_synthetic.py     # 50 synthetic resumes
```

**Never seed real resumes.** The synthetic-data marker is enforced in CI, and
the deployed instance carries the same decision-support banner as local.

## Status

⚠️ **Not yet performed.** Everything above is prepared and the workflow is
written, but no cloud deployment has been made — the accounts are yours to
create. Nothing in this file should be read as "this ran successfully"; when it
does, this section gets replaced with what actually happened, in the style of
the [runbook](runbook.md).

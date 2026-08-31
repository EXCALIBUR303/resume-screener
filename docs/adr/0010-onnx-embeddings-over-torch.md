# ADR-0010 — ONNX embeddings rather than sentence-transformers

**Status:** accepted · **Date:** 2026-08-31

*(This file was written late: the code referenced ADR-0010 in a comment before the
decision was recorded, and the gap was found by a README that claimed a count of
ADRs the directory did not contain. Numbering gaps are a smell.)*

## Context

M5 needs `BAAI/bge-small-en-v1.5` embeddings. The obvious library is
`sentence-transformers`, which pulls in PyTorch.

The parse and AI workers both run in containers built on every change, on a
laptop, with a hard project constraint of zero ongoing cost and no hidden
infrastructure. Image size is a real budget here, not a detail.

## Measured

| | installed size |
|---|---|
| `fastembed` + `onnxruntime` + `tokenizers` + `numpy` | **109 MB** |
| `torch` alone (CPU wheel) | ~800 MB |

Same weights, same 384 dimensions, same outputs. Embedding two documents took
10 ms after a 15 s cold start.

## Decision

`fastembed`, which runs the model through ONNX Runtime.

The model is **baked into the image at build time** (`FASTEMBED_CACHE_PATH=/opt/models`),
because the workers have no network at runtime and fastembed would otherwise try
to fetch weights on first use — which fails silently in the worst possible place,
mid-job.

## Consequences

- `torch` never enters the dependency tree, so CI installs and container builds
  stay minutes rather than tens of minutes.
- `onnxruntime` publishes `manylinux_2_28` wheels but **not** `manylinux2014`, so
  the hash-pinned lock targets `x86_64-manylinux_2_28`. Getting this wrong fails
  at lock time with a confusing resolution error rather than at runtime.
- ONNX reserves far more *virtual* address space than it resides, which made a
  1 GB `RLIMIT_AS` kill the AI worker with `std::bad_alloc`. The rlimit is now
  scoped to the parse pool, where it guards against decompression bombs; the AI
  pool is bounded by the container `mem_limit` instead.
- Switching back to `sentence-transformers` would mean reverting all three of
  those, so this is a more load-bearing choice than "which library".

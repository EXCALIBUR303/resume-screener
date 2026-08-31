# Security

## Reporting

This is a portfolio project, not a service. If you find a vulnerability, open a GitHub issue —
there is no production deployment holding anyone's data, so there is nothing to embargo.

## Scope

The system is **decision support only** and must not be used with real candidate data. It has
not been penetration tested or audited for bias.

## What is enforced, and where

| Guarantee | Enforced by | Test |
|---|---|---|
| No endpoint escapes authorization | route-table enumeration | `test_authz_matrix.py` (AC-6/7) |
| No raw PII reaches a model or log | egress-scanning fake provider | `test_pii_egress.py` (AC-3) |
| Malicious uploads are rejected | 18-case corpus + 720-mutation fuzz | `test_upload_corpus.py` (AC-8) |
| Injection never pays | 40-case corpus, asserted on the fused score | `test_ac9_injection_suite.py` (AC-9) |
| Audit rows cannot be altered | database grants **and** a trigger | `test_integration_db.py` |
| Erasure leaves no residue | scripted sweep of every table and the blob store | `test_integration_db.py` (AC-14) |
| The parse worker has no network | compose assertion + live probe | `test_parse_corpus.py` |

## Known limitations

- Rolling our own auth is normally poor practice. It is mitigated with argon2id, a pinned JWT
  algorithm, database-sourced roles, rotating refresh tokens with reuse detection, and an
  exhaustive authorization matrix — but OAuth remains the safer default.
- The injection detector is a **signal**, not the primary control. Evidence verification does the
  load-bearing work.
- ClamAV is an optional compose profile. `scan_status` records `"skipped"` honestly when it is off.

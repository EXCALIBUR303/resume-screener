# ADR-0020 — Attribute-scoped match access, and SBOM/signing prepared but not published

**Status:** accepted · **Date:** 2026-09-01 · **Extends** the role matrix in `security/roles.py`

## The gap the role matrix could not express

`HIRING_MANAGER` holds `MATCH_READ`, and the ranked-matches query filtered on
`org_id` alone. So a hiring manager brought in to fill one position could read
the ranked candidates, evidence quotes and scores for **every** position in the
organisation.

That is not a bug in the matrix; it is the limit of what a matrix can say. RBAC
answers "may this role read matches". The question here is "may *this* hiring
manager read the candidates for *that* job", and the answer depends on an
attribute of the resource. A role has no vocabulary for *which* job.

The rule added is deliberately narrow:

> A hiring manager may read matches only for job postings they are assigned to.

Owners, admins and recruiters are unchanged. Recruiters ingest candidates and
already see them, and scoping an owner out of their own tenant's data would be
theatre rather than a control. `security/abac.py` lists the unscoped roles
explicitly, and a test fails when a role is added to the system without being
classified — the safe default is "scoped", but only if someone notices.

## The scope is a WHERE clause, never a filter over results

This is the decision that matters. Fetching every match and dropping the rows
the caller may not see:

- still tells them how many there were,
- still consumes their page budget, so a `limit=50` returns four rows and they
  learn 46 exist,
- and turns any count into an existence oracle for roles they were never meant
  to know about.

`visible_jobs_condition` returns SQL — a correlated `EXISTS` against
`job_assignments` — and the tests assert on the compiled statement so that a
future refactor into a Python filter fails rather than passes quietly.

It returns an always-true condition for unscoped roles rather than `None`. Given
`None`, a call site handles "no restriction" by leaving the filter off, and that
is the same code path as forgetting it. The function decides what the condition
says; the call site always applies one.

## Access is a row

`job_assignments` rather than a role, because "which job" is not a property of a
person. Revocation is a `DELETE`: no token to expire, no cache to invalidate,
and an integration test that removes an assignment and re-runs the same query.

Granting is gated on `JOB_WRITE`, not `MATCH_READ`. **The person who may read a
shortlist is not automatically the person who may decide who else can.** Both
grant and revoke write an audit event naming who was granted or revoked.

The endpoint returns the same 404 for "no such job" and "no such user in your
tenant", because distinguishing them is an enumeration oracle for other tenants'
user ids — the same reasoning already applied to the 403/404 split elsewhere.

## SBOM, and signing that has not been run

An SBOM per build is what makes "was this build affected by CVE-X" answerable
after the fact. Trivy was already in the security workflow and emits CycloneDX,
so this cost one step rather than a new dependency: three SBOMs per run — the
API image, the worker image, and the repository's pinned dependency graphs —
retained as artifacts for 90 days.

Signing is a separate matter, because **a signature is only meaningful for an
artifact in a registry**, and publishing images to GHCR puts something into the
world under the repository owner's account. `release.yml` is written and is
triggered only by a version tag or a manual dispatch. It has **never run.**

What it does when someone chooses to run it:

- **cosign keyless.** The runner exchanges its GitHub OIDC token for a
  short-lived Fulcio certificate. There is no private key to store, rotate or
  leak, which is the whole reason for signing this way rather than with a
  keypair in a repository secret.
- **Signs the digest, never the tag.** A tag is mutable; signing `:latest` says
  nothing about what `:latest` will point at tomorrow.
- **Attests the SBOM rather than attaching it.** An unsigned SBOM sitting beside
  an image answers "what does the author claim is in it", which is a different
  question from "what is in it".
- **Verifies what it just published**, in the same run, against the expected
  certificate identity and issuer. A signing step that never verifies is a
  signing step nobody has checked.

Recorded as prepared-not-run, in the same way `docs/deployment.md` records that
no cloud deployment has been performed.

## Consequences

- One new table and three routes, all covered by the AC-6 authorization matrix
  (verified by listing them, not assumed).
- A hiring manager with no assignments now sees an empty list where they
  previously saw the whole tenant. That is the point, and it is a behaviour
  change for any existing hiring-manager account.
- The scoping subquery filters on `user_id` and correlates on `job_id`, which is
  the composite index the migration adds.

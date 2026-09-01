"""Attribute-based scoping on top of the role matrix.

RBAC answers "may this role read matches at all". It cannot answer "may *this*
hiring manager read the candidates for *that* role", because the answer depends
on an attribute of the resource rather than on the caller's role alone.

That gap was live. `HIRING_MANAGER` holds `MATCH_READ`, so a hiring manager
hired to fill one position could read the ranked candidates for every other
position in the organisation. Nothing in the role matrix could express the
restriction, because the matrix has no vocabulary for *which* job.

The rule this module adds is deliberately narrow:

    A hiring manager may read matches only for job postings they are assigned to.

Owners, admins and recruiters are unchanged — recruiters ingest candidates and
already see them, and scoping an owner out of their own tenant's data would be
theatre rather than a control.

**Scoping is a WHERE clause, never a filter over results.** Fetching everything
and dropping the rows the caller may not see still tells them how many there
were, still consumes their page budget, and turns any `count` into an oracle
for the existence of roles they were never meant to know about. `visible_jobs`
returns a SQL condition for that reason.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ColumnElement, Select, exists, select
from sqlalchemy import true as sql_true
from sqlalchemy.sql.elements import SQLCoreOperations

from screener_api.models import JobAssignment
from screener_api.security.deps import Actor
from screener_api.security.roles import Role

# Roles whose match access is unrestricted within their own tenant.
UNSCOPED_ROLES = frozenset({Role.ORG_OWNER, Role.ORG_ADMIN, Role.RECRUITER})


def has_unscoped_match_access(actor: Actor) -> bool:
    return bool(actor.roles & {str(r) for r in UNSCOPED_ROLES})


def visible_jobs_condition(
    actor: Actor, job_id_column: SQLCoreOperations[uuid.UUID]
) -> ColumnElement[bool]:
    """A SQL condition restricting rows to job postings this actor may see.

    Returns an always-true condition for unscoped roles rather than `None`, so
    a call site cannot handle "no restriction" by leaving the filter off — the
    condition is applied unconditionally and it is this function's job to decide
    what it says.
    """
    if has_unscoped_match_access(actor):
        return sql_true()

    return exists(
        select(JobAssignment.id).where(
            JobAssignment.job_id == job_id_column,
            JobAssignment.user_id == actor.user_id,
        )
    )


def scope_to_visible_jobs(
    statement: Select[Any], actor: Actor, job_id_column: SQLCoreOperations[uuid.UUID]
) -> Select[Any]:
    return statement.where(visible_jobs_condition(actor, job_id_column))

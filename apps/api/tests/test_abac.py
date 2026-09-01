"""Attribute-scoped match access.

RBAC answers "may this role read matches at all". It cannot answer "may THIS
hiring manager read the candidates for THAT job", and until this module existed
the answer was yes for every job in the tenant.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from screener_api.models import JobAssignment, Match
from screener_api.security.abac import (
    UNSCOPED_ROLES,
    has_unscoped_match_access,
    scope_to_visible_jobs,
    visible_jobs_condition,
)
from screener_api.security.deps import Actor
from screener_api.security.roles import Permission, Role, permissions_for


def _actor(*roles: Role) -> Actor:
    names = frozenset(str(r) for r in roles)
    return Actor(
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        roles=names,
        permissions=permissions_for(names),
    )


def test_a_hiring_manager_can_read_matches_at_all() -> None:
    """The premise. If this were false the scope would be redundant."""
    assert _actor(Role.HIRING_MANAGER).can(Permission.MATCH_READ)


def test_a_hiring_manager_is_the_only_scoped_role() -> None:
    assert not has_unscoped_match_access(_actor(Role.HIRING_MANAGER))
    for role in UNSCOPED_ROLES:
        assert has_unscoped_match_access(_actor(role)), role


def test_a_user_with_both_roles_keeps_the_wider_access() -> None:
    """Someone who is a recruiter AND a hiring manager already sees every
    candidate through the recruiter role. Scoping them would be theatre."""
    assert has_unscoped_match_access(_actor(Role.HIRING_MANAGER, Role.RECRUITER))


def test_the_scope_is_sql_not_a_filter_over_results() -> None:
    """Filtering after the query still leaks the count, still consumes the
    caller's page budget, and turns any total into an existence oracle for
    roles they were never meant to know about."""
    statement = scope_to_visible_jobs(select(Match), _actor(Role.HIRING_MANAGER), Match.job_id)
    compiled = str(statement.compile())
    assert "EXISTS" in compiled
    assert "job_assignments" in compiled


def test_an_unscoped_role_still_gets_a_condition_rather_than_none() -> None:
    """Returning None for "no restriction" would let a call site handle it by
    leaving the filter off entirely, which is the same code path as forgetting
    it. The function decides what the condition says; the call site always
    applies one."""
    condition = visible_jobs_condition(_actor(Role.ORG_ADMIN), Match.job_id)
    assert condition is not None
    assert "true" in str(condition.compile()).lower()


def test_the_scoped_condition_correlates_on_the_job_column_it_was_given() -> None:
    """A subquery that ignored the outer job id would be always-true for anyone
    assigned to any job at all — a scope that looks applied and is not."""
    compiled = str(visible_jobs_condition(_actor(Role.HIRING_MANAGER), Match.job_id).compile())
    assert "matches.job_id" in compiled
    assert "job_assignments.user_id" in compiled


def test_an_auditor_has_no_match_access_to_scope() -> None:
    auditor = _actor(Role.AUDITOR)
    assert not auditor.can(Permission.MATCH_READ)
    # And is not accidentally granted unscoped access by omission.
    assert not has_unscoped_match_access(auditor)


@pytest.mark.parametrize("role", list(Role))
def test_every_role_is_classified(role: Role) -> None:
    """A role added later must be a deliberate decision, not a default.

    Anything not in UNSCOPED_ROLES is scoped, which is the safe direction — but
    only if someone notices. This test fails when a new role appears without a
    line in the table below.
    """
    scoped = {Role.HIRING_MANAGER, Role.AUDITOR}
    assert role in UNSCOPED_ROLES or role in scoped, (
        f"{role} is neither listed as unscoped nor deliberately scoped"
    )


def test_the_assignment_is_a_row_not_a_role() -> None:
    """ "Which job" is not a property of a person, so it cannot live in the role
    matrix. The model exists to say so."""
    assignment = JobAssignment(
        id=uuid.uuid4(), org_id=uuid.uuid4(), job_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    assert assignment.job_id is not None and assignment.user_id is not None


def test_every_route_that_selects_matches_applies_the_scope() -> None:
    """A guard against the filter being dropped in a refactor.

    Losing an authorization filter is silent: the endpoint still returns 200,
    the shape of the response is unchanged, and the only difference is that a
    hiring manager can suddenly see the whole tenant. Nothing else in the suite
    would notice, because every other test either has an unscoped role or
    checks the condition in isolation.

    Same shape as `test_the_route_walker_actually_finds_routes` in the authz
    matrix: a test whose job is to make a specific way of being wrong loud.
    """
    import pathlib

    routers = pathlib.Path(__file__).resolve().parents[1] / "src" / "screener_api" / "routers"
    offenders: list[str] = []

    for path in sorted(routers.glob("*.py")):
        source = path.read_text()
        queries = source.count("select(Match")
        if not queries:
            continue
        # Counting CALLS, not the name. The first version of this test looked
        # for the bare identifier and passed with the filter deleted, because
        # the import line still mentioned it — a guard that could not fire,
        # which is the defect ADR-0017 finding 3 is about. Counting also
        # catches a SECOND match query added without a scope.
        applied = source.count("visible_jobs_condition(")
        if applied < queries:
            offenders.append(f"{path.name} ({queries} queries, {applied} scoped)")

    assert not offenders, (
        f"{', '.join(offenders)}: a match query is not scoped. Every one must be "
        f"filtered in SQL, never over the results — see ADR-0020."
    )

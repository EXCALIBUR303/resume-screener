"""AC-6 and AC-7: nothing escapes the authorization matrix.

AC-6 is the load-bearing test. It enumerates the live FastAPI route table and
fails if a route is neither gated nor explicitly listed as public. A new
endpoint therefore *cannot merge* without someone making an authorization
decision about it — which is the only way this stays true under deadline
pressure.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute

from screener_api.main import create_app
from screener_api.security.roles import ROLE_PERMISSIONS, Permission, Role, permissions_for
from screener_api.settings import Settings

# Routes intentionally reachable without authentication. Every entry needs a
# reason, because this list is the only escape hatch from AC-6.
PUBLIC_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/healthz"): "liveness probe; returns no data",
    ("GET", "/readyz"): "readiness probe; returns component status only",
    ("POST", "/auth/login"): "issues credentials; cannot require them",
    ("POST", "/auth/refresh"): "authenticated by the refresh token itself",
}

# Routes that require a logged-in user but no specific permission.
AUTHENTICATED_ONLY: dict[tuple[str, str], str] = {
    ("POST", "/auth/logout"): "any authenticated user may end their own session",
    ("GET", "/auth/me"): "returns only the caller's own identity",
}


def _walk(container: object, seen: set[int]) -> list[APIRoute]:
    """Collect every APIRoute reachable from the app.

    This must not assume routes are flat. Recent FastAPI wraps `include_router`
    results in an `_IncludedRouter` proxy, so a naive scan of `app.routes` finds
    only the routes declared directly on the app — and AC-6 then passes while
    checking nothing. That failure mode is the reason
    `test_the_route_walker_actually_finds_routes` exists below.
    """
    if id(container) in seen:
        return []
    seen.add(id(container))

    found: list[APIRoute] = []
    if isinstance(container, APIRoute):
        return [container]
    for attr in ("routes", "original_router"):
        child = getattr(container, attr, None)
        if child is None:
            continue
        for item in child if isinstance(child, list) else [child]:
            found.extend(_walk(item, seen))
    return found


def _routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    out = []
    for route in _walk(app, set()):
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            out.append((method, route.path, route))
    return out


def _declared_permission(route: APIRoute) -> Permission | None:
    for dep in route.dependant.dependencies:
        perm = getattr(dep.call, "__required_permission__", None)
        if perm is not None:
            return perm  # type: ignore[no-any-return]
    return None


def _requires_auth(route: APIRoute) -> bool:
    """True if any dependency in the tree resolves the current actor."""
    seen, stack = set(), list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        name = getattr(dep.call, "__name__", "")
        if name in {"get_actor", "_check"}:
            return True
        if id(dep) not in seen:
            seen.add(id(dep))
            stack.extend(dep.dependencies)
    return False


APP = create_app(Settings(app_env="dev", postgres_password="x", app_kek="x", jwt_secret="x"))


def test_the_route_walker_actually_finds_routes() -> None:
    """Guards the guard. A matrix test that enumerates nothing passes trivially,
    which is exactly what happened before the walker recursed into included
    routers. Pin the count so a future FastAPI change cannot silently empty it."""
    found = _routes(APP)
    paths = {p for _, p, _ in found}
    assert len(found) >= 8, f"route walker found only {len(found)} routes: {sorted(paths)}"
    for expected in (
        "/healthz",
        "/readyz",
        "/auth/login",
        "/auth/me",
        "/admin/users",
        "/admin/audit",
        "/admin/audit/verify",
    ):
        assert expected in paths, f"{expected} not discovered by the walker"


def test_every_route_has_an_authorization_decision() -> None:
    """AC-6: 100% route coverage. This is the test that makes the matrix real."""
    undecided = []
    for method, path, route in _routes(APP):
        key = (method, path)
        if key in PUBLIC_ROUTES or key in AUTHENTICATED_ONLY:
            continue
        if _declared_permission(route) is None:
            undecided.append(f"{method} {path}")
    assert not undecided, (
        "These routes declare no permission and are not listed as public.\n"
        "Add `requires(Permission.X)` to the route, or add it to PUBLIC_ROUTES / "
        "AUTHENTICATED_ONLY with a reason:\n  " + "\n  ".join(undecided)
    )


def test_public_and_authenticated_lists_have_no_stale_entries() -> None:
    """A deleted route must not linger on the allowlist — that would silently
    pre-approve a future route that reuses the path."""
    live = {(m, p) for m, p, _ in _routes(APP)}
    stale = (set(PUBLIC_ROUTES) | set(AUTHENTICATED_ONLY)) - live
    assert not stale, f"Allowlist entries for routes that no longer exist: {sorted(stale)}"


def test_public_routes_really_are_unauthenticated() -> None:
    for method, path, route in _routes(APP):
        if (method, path) in PUBLIC_ROUTES:
            assert not _requires_auth(route), f"{method} {path} is listed public but needs auth"


def test_gated_routes_really_do_require_auth() -> None:
    for method, path, route in _routes(APP):
        if (method, path) in PUBLIC_ROUTES:
            continue
        assert _requires_auth(route), f"{method} {path} is not public but requires no actor"


# ---- AC-7: the role x permission matrix itself ----

EXPECTED = {
    Role.ORG_OWNER: {"all"},
    Role.ORG_ADMIN: {
        Permission.USER_READ,
        Permission.USER_WRITE,
        Permission.AUDIT_READ,
        Permission.ORG_ADMIN_SETTINGS,
        Permission.DLQ_MANAGE,
    },
    Role.RECRUITER: {Permission.RESUME_WRITE, Permission.JOB_WRITE, Permission.INTERVIEW_WRITE},
    Role.HIRING_MANAGER: {Permission.MATCH_READ, Permission.JOB_READ},
    Role.AUDITOR: {Permission.AUDIT_READ},
}

DENIED = {
    Role.RECRUITER: {Permission.AUDIT_READ, Permission.USER_WRITE, Permission.DLQ_MANAGE},
    # A hiring manager reads results but never ingests candidates — fewer people
    # touching raw resumes is itself a privacy control.
    Role.HIRING_MANAGER: {
        Permission.RESUME_WRITE,
        Permission.RESUME_READ,
        Permission.USER_WRITE,
        Permission.AUDIT_READ,
    },
    # The auditor sees that things happened, never what they contained.
    Role.AUDITOR: {
        Permission.RESUME_READ,
        Permission.MATCH_READ,
        Permission.USER_READ,
        Permission.JOB_READ,
        Permission.INTERVIEW_READ,
    },
    Role.ORG_ADMIN: {Permission.RESUME_WRITE, Permission.INTERVIEW_WRITE},
}


def test_each_role_has_the_permissions_it_should() -> None:
    for role, expected in EXPECTED.items():
        granted = ROLE_PERMISSIONS[role]
        if expected == {"all"}:
            assert granted == frozenset(Permission)
            continue
        missing = {p for p in expected if p not in granted}  # type: ignore[comparison-overlap]
        assert not missing, f"{role} is missing {missing}"


def test_each_role_is_denied_what_it_should_be() -> None:
    for role, denied in DENIED.items():
        granted = ROLE_PERMISSIONS[role]
        leaked = {p for p in denied if p in granted}
        assert not leaked, f"{role} unexpectedly has {leaked}"


def test_every_role_appears_in_the_matrix() -> None:
    assert set(ROLE_PERMISSIONS) == set(Role)


def test_unknown_roles_grant_nothing() -> None:
    """A role string in the database that the code does not recognise must be
    inert, never a wildcard."""
    assert permissions_for(frozenset({"superuser", "root", "*", ""})) == frozenset()


def test_known_and_unknown_roles_combine_safely() -> None:
    assert permissions_for(frozenset({"auditor", "root"})) == ROLE_PERMISSIONS[Role.AUDITOR]


def test_only_the_owner_holds_every_permission() -> None:
    for role, granted in ROLE_PERMISSIONS.items():
        if role is not Role.ORG_OWNER:
            assert granted != frozenset(Permission), f"{role} has owner-level access"

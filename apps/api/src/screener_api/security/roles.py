"""Role definitions and the permission matrix.

Roles are data, not code branches: every route declares the permission it needs,
and this table is the single place that maps roles to permissions. Adding a role
means editing one dict, not hunting for `if user.role ==` across the codebase.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ORG_OWNER = "org_owner"
    ORG_ADMIN = "org_admin"
    RECRUITER = "recruiter"
    HIRING_MANAGER = "hiring_manager"
    AUDITOR = "auditor"


class Permission(StrEnum):
    USER_READ = "user:read"
    USER_WRITE = "user:write"
    JOB_READ = "job:read"
    JOB_WRITE = "job:write"
    RESUME_READ = "resume:read"
    RESUME_WRITE = "resume:write"
    MATCH_READ = "match:read"
    INTERVIEW_READ = "interview:read"
    INTERVIEW_WRITE = "interview:write"
    AUDIT_READ = "audit:read"
    ORG_ADMIN_SETTINGS = "org:settings"
    DLQ_MANAGE = "dlq:manage"


P = Permission

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ORG_OWNER: frozenset(Permission),  # everything, including deletion
    Role.ORG_ADMIN: frozenset(
        {
            P.USER_READ,
            P.USER_WRITE,
            P.JOB_READ,
            P.JOB_WRITE,
            P.RESUME_READ,
            P.MATCH_READ,
            P.INTERVIEW_READ,
            P.AUDIT_READ,
            P.ORG_ADMIN_SETTINGS,
            P.DLQ_MANAGE,
        }
    ),
    Role.RECRUITER: frozenset(
        {
            P.JOB_READ,
            P.JOB_WRITE,
            P.RESUME_READ,
            P.RESUME_WRITE,
            P.MATCH_READ,
            P.INTERVIEW_READ,
            P.INTERVIEW_WRITE,
        }
    ),
    # Reads results, cannot ingest candidates. The separation is the point:
    # fewer people touching raw resumes is a privacy control.
    Role.HIRING_MANAGER: frozenset(
        {
            P.JOB_READ,
            P.MATCH_READ,
            P.INTERVIEW_READ,
        }
    ),
    # Sees that things happened, never what they contained.
    Role.AUDITOR: frozenset({P.AUDIT_READ}),
}


def permissions_for(role_names: frozenset[str]) -> frozenset[Permission]:
    granted: set[Permission] = set()
    for name in role_names:
        try:
            granted |= ROLE_PERMISSIONS[Role(name)]
        except ValueError:
            continue  # unknown role in the database grants nothing
    return frozenset(granted)

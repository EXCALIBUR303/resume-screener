"""Tenant-scoped data access. The only place the app reads domain rows.

Rule D-11: there is no unscoped accessor. Every read takes an ``Actor`` and
filters by ``org_id`` in SQL. A Semgrep rule bans ``session.get(Model, id)``
outside this module so the rule cannot be bypassed by accident.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.models import Base, User
from screener_api.security.deps import Actor


class TenantScopedRepo[ModelT: Base]:
    """Base repository. Subclasses set ``model``; scoping is not overridable."""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _scoped(self, actor: Actor) -> Select[tuple[ModelT]]:
        org_column = getattr(self.model, "org_id", None)
        if org_column is None:
            raise TypeError(
                f"{self.model.__name__} has no org_id; it cannot be tenant-scoped. "
                "Do not access it through this repository."
            )
        return select(self.model).where(org_column == actor.org_id)

    async def get(self, actor: Actor, obj_id: uuid.UUID) -> ModelT | None:
        """Returns None for a row in another org — indistinguishable from absent,
        so the endpoint cannot be used to probe for existence across tenants."""
        stmt = self._scoped(actor).where(self.model.id == obj_id)  # type: ignore[attr-defined]
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(self, actor: Actor, *, limit: int = 50, offset: int = 0) -> list[ModelT]:
        stmt = self._scoped(actor).limit(min(limit, 200)).offset(offset)
        return list((await self.session.execute(stmt)).scalars().all())

    async def add(self, actor: Actor, **fields: Any) -> ModelT:
        # org_id is taken from the actor, never from the request body — this is
        # what stops mass assignment from writing into another tenant.
        fields.pop("org_id", None)
        obj = self.model(org_id=actor.org_id, **fields)
        self.session.add(obj)
        await self.session.flush()
        return obj


class UserRepo(TenantScopedRepo[User]):
    model = User

    async def by_email(self, org_id: uuid.UUID, email: str) -> User | None:
        """Login path only: there is no Actor yet, so the org is explicit."""
        from sqlalchemy.orm import selectinload

        stmt = (
            select(User)
            .where(User.org_id == org_id, User.email == email.lower().strip())
            .options(selectinload(User.roles))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

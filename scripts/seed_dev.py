"""Seed a dev organisation with one user per role. Synthetic data only."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from screener_api.models import Organization, User, UserRole  # noqa: E402
from screener_api.security import audit  # noqa: E402
from screener_api.security.passwords import hash_password  # noqa: E402
from screener_api.security.roles import Role  # noqa: E402
from screener_api.settings import get_settings  # noqa: E402

PASSWORD = "dev-password-not-for-production"  # noqa: S105


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.dsn)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        org = (
            await session.execute(select(Organization).where(Organization.name == "Acme Talent"))
        ).scalar_one_or_none()
        if org is None:
            org = Organization(id=uuid.uuid4(), name="Acme Talent")
            session.add(org)
            await session.flush()

        for role in Role:
            email = f"{role.value}@example.com"
            existing = (
                await session.execute(
                    select(User).where(User.org_id == org.id, User.email == email)
                )
            ).scalar_one_or_none()
            if existing:
                continue
            user = User(
                id=uuid.uuid4(), org_id=org.id, email=email,
                display_name=role.value.replace("_", " ").title(),
                password_hash=hash_password(PASSWORD),
            )
            session.add(user)
            await session.flush()
            session.add(UserRole(id=uuid.uuid4(), user_id=user.id, role=role.value))
            await audit.record(
                session, action="user.created", resource_type="user",
                resource_id=str(user.id), org_id=org.id, meta={"role": role.value},
            )

        await session.commit()
        print(f"ORG_ID={org.id}")

    await engine.dispose()


asyncio.run(main())

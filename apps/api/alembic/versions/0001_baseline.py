"""Baseline: extensions and the audit-chain guarantee this project depends on.

Revision ID: 0001_baseline
Revises:
"""

from __future__ import annotations

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pgvector: the vector store. Confirmed 0.8.6 on pgvector/pgvector:pg16.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # pgcrypto: gen_random_uuid() and digest() for the audit hash chain.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    # A single low-privilege role the app connects as. Ownership of tables stays
    # with the migration role, so the app cannot ALTER or DROP them. Grants for
    # each table are issued by the migration that creates it, which is what lets
    # M1 deny UPDATE/DELETE on audit_events specifically.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'screener_app') THEN
                CREATE ROLE screener_app NOLOGIN;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP ROLE IF EXISTS screener_app")
    op.execute("DROP EXTENSION IF EXISTS pgcrypto")
    op.execute("DROP EXTENSION IF EXISTS vector")

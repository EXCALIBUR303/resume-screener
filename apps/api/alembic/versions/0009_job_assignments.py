"""M14: hiring-panel assignments, for attribute-scoped match access.

Revision ID: 0009_job_assignments
Revises: 0008_outbox_webhooks
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_job_assignments"
down_revision: str | None = "0008_outbox_webhooks"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "job_assignments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "org_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "job_id", UUID, sa.ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("assigned_by", UUID, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("job_id", "user_id", name="uq_job_assignment"),
    )
    op.create_index("ix_job_assignments_org_id", "job_assignments", ["org_id"])
    op.create_index("ix_job_assignments_job_id", "job_assignments", ["job_id"])
    # The scoping subquery filters on user_id and correlates on job_id, so this
    # is the index it actually uses.
    op.create_index("ix_job_assignments_user_job", "job_assignments", ["user_id", "job_id"])

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON job_assignments TO screener_app")


def downgrade() -> None:
    op.drop_table("job_assignments")

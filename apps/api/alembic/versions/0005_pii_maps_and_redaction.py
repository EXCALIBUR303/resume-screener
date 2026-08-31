"""M4: encrypted PII maps and redaction bookkeeping.

Revision ID: 0005_pii_maps
Revises: 0004_queue_and_text
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_pii_maps"
down_revision: str | None = "0004_queue_and_text"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "pii_maps",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "org_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "resume_id", UUID, sa.ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("entity_counts", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("resume_id", name="uq_pii_map_resume"),
    )
    op.create_index("ix_pii_maps_org_id", "pii_maps", ["org_id"])
    op.create_index("ix_pii_maps_resume_id", "pii_maps", ["resume_id"])

    op.add_column("resumes", sa.Column("redacted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "resumes",
        sa.Column("needs_manual_review", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.add_column("candidates", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON pii_maps TO screener_app")


def downgrade() -> None:
    op.drop_column("candidates", "purged_at")
    op.drop_column("resumes", "needs_manual_review")
    op.drop_column("resumes", "redacted_at")
    op.drop_table("pii_maps")

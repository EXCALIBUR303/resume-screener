"""M3: job queue and extracted resume text.

Revision ID: 0004_queue_and_text
Revises: 0003_files_and_resumes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_queue_and_text"
down_revision: str | None = "0003_files_and_resumes"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "job_queue",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("org_id", UUID, nullable=True),
        sa.Column("job_type", sa.String(40), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="5"),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(80), nullable=True),
        sa.Column("error_class", sa.String(40), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_job_idempotency"),
    )
    # Partial index: claims only ever look at pending rows, and the table becomes
    # mostly completed ones.
    op.execute(
        "CREATE INDEX ix_job_queue_claimable ON job_queue (job_type, run_after) "
        "WHERE status = 'pending'"
    )
    op.create_index("ix_job_queue_lease", "job_queue", ["status", "locked_at"])

    op.create_table(
        "resume_texts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "org_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "resume_id", UUID, sa.ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("raw_text", sa.Text, nullable=False),
        sa.Column("text_redacted", sa.Text, nullable=True),
        sa.Column("char_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sections", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("extractor", sa.String(30), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("resume_id", name="uq_resume_text"),
    )
    op.create_index("ix_resume_texts_org_id", "resume_texts", ["org_id"])
    op.create_index("ix_resume_texts_resume_id", "resume_texts", ["resume_id"])

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON job_queue, resume_texts TO screener_app")


def downgrade() -> None:
    op.drop_table("resume_texts")
    op.drop_table("job_queue")

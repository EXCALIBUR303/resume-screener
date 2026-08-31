"""M6 wiring: job postings and scored matches.

Revision ID: 0007_jobs_matches
Revises: 0006_chunks_vectors
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_jobs_matches"
down_revision: str | None = "0006_chunks_vectors"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "job_postings",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "org_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("required_skills", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("nice_to_have", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("hard_requirements", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("min_years", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_job_postings_org_id", "job_postings", ["org_id"])

    op.create_table(
        "matches",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "org_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "job_id", UUID, sa.ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "resume_id", UUID, sa.ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("components", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("rubric", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("evidence", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("unmet_requirements", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("degraded", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("partially_supported", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("injection_suspected", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("model_id", sa.String(80), nullable=False),
        sa.Column("prompt_version", sa.String(40), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # A re-score under a different prompt or model is a NEW row, never an
        # overwrite: a score is not interpretable without knowing what made it.
        sa.UniqueConstraint(
            "job_id", "resume_id", "prompt_version", "model_id", name="uq_match_run"
        ),
    )
    op.create_index("ix_matches_org_id", "matches", ["org_id"])
    op.create_index("ix_matches_job_id", "matches", ["job_id"])
    op.create_index("ix_matches_resume_id", "matches", ["resume_id"])
    op.create_index("ix_matches_job_score", "matches", ["job_id", "score"])

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON job_postings, matches TO screener_app")


def downgrade() -> None:
    op.drop_table("matches")
    op.drop_table("job_postings")

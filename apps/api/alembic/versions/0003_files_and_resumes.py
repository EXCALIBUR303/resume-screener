"""M2: files, candidates, resumes.

Revision ID: 0003_files_and_resumes
Revises: 0002_identity_and_audit
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_files_and_resumes"
down_revision: str | None = "0002_identity_and_audit"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "org_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(140), nullable=False),
        sa.Column("byte_size", sa.Integer, nullable=False),
        sa.Column("mime_declared", sa.String(180), nullable=True),
        sa.Column("mime_sniffed", sa.String(180), nullable=False),
        sa.Column("mime_resolved", sa.String(180), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False, server_default=""),
        sa.Column("page_count", sa.Integer, nullable=True),
        sa.Column("scan_status", sa.String(20), nullable=False, server_default="skipped"),
        sa.Column("scan_engine", sa.String(60), nullable=True),
        sa.Column("is_quarantined", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("uploaded_by", UUID, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("org_id", "sha256", name="uq_files_org_sha"),
    )
    op.create_index("ix_files_org_id", "files", ["org_id"])

    op.create_table(
        "candidates",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "org_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("pseudonym", sa.String(60), nullable=False),
        sa.Column("external_ref", sa.String(120), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_candidates_org_id", "candidates", ["org_id"])

    op.create_table(
        "resumes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "org_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "candidate_id", UUID, sa.ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
        ),
        # RESTRICT, not CASCADE: a file must not vanish while a resume still
        # references it. Erasure removes the resume first, then the blob.
        sa.Column("file_id", UUID, sa.ForeignKey("files.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("parse_status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("parse_error", sa.String(200), nullable=True),
        sa.Column("ocr_used", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("ocr_confidence", sa.Integer, nullable=True),
        sa.Column("language", sa.String(12), nullable=True),
        sa.Column("pipeline_version", sa.String(20), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_resumes_org_id", "resumes", ["org_id"])
    op.create_index("ix_resumes_candidate_id", "resumes", ["candidate_id"])

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON files, candidates, resumes TO screener_app")


def downgrade() -> None:
    op.drop_table("resumes")
    op.drop_table("candidates")
    op.drop_table("files")

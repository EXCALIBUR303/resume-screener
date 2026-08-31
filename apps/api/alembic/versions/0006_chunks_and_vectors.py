"""M5: chunk table with vector and full-text indexes.

Revision ID: 0006_chunks_vectors
Revises: 0005_pii_maps
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_chunks_vectors"
down_revision: str | None = "0005_pii_maps"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
EMBEDDING_DIM = 384


def upgrade() -> None:
    op.create_table(
        "resume_chunks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "org_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "resume_id", UUID, sa.ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        # The redacted text only. Nothing in this table has ever seen raw PII,
        # which is what lets it be indexed and searched freely.
        sa.Column("text_redacted", sa.Text, nullable=False),
        # Offsets into resume_texts.text_redacted. Evidence verification in M6
        # depends on these being exact.
        sa.Column("char_start", sa.Integer, nullable=False),
        sa.Column("char_end", sa.Integer, nullable=False),
        sa.Column("section", sa.String(40), nullable=True),
        sa.Column("embedding", postgresql.ARRAY(sa.REAL), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("resume_id", "chunk_index", name="uq_chunk_per_resume"),
    )
    # Swap the placeholder array column for a real pgvector column.
    op.execute(
        f"ALTER TABLE resume_chunks ALTER COLUMN embedding TYPE vector({EMBEDDING_DIM}) "
        f"USING embedding::vector({EMBEDDING_DIM})"
    )

    op.create_index("ix_chunks_org_id", "resume_chunks", ["org_id"])
    op.create_index("ix_chunks_resume_id", "resume_chunks", ["resume_id"])

    # Full-text: a generated column so the index can never drift from the text.
    op.execute(
        "ALTER TABLE resume_chunks ADD COLUMN tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', text_redacted)) STORED"
    )
    op.execute("CREATE INDEX ix_chunks_tsv ON resume_chunks USING GIN (tsv)")

    # HNSW for approximate nearest neighbour. Cosine distance, because bge
    # vectors are normalised and cosine is what the model was trained for.
    op.execute(
        "CREATE INDEX ix_chunks_embedding ON resume_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    # Composite index supporting the tenant predicate that every query carries.
    op.create_index("ix_chunks_org_resume", "resume_chunks", ["org_id", "resume_id"])

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON resume_chunks TO screener_app")


def downgrade() -> None:
    op.drop_table("resume_chunks")

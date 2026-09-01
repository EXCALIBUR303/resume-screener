"""M14: transactional outbox and webhook endpoints.

Revision ID: 0008_outbox_webhooks
Revises: 0007_jobs_matches
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_outbox_webhooks"
down_revision: str | None = "0007_jobs_matches"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "org_id",
            UUID,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("description", sa.String(200), nullable=False, server_default=""),
        # AES-256-GCM envelope, org_id as AAD. The signing key is never stored
        # or returned in clear; it is shown to the operator once, at creation.
        sa.Column("secret_ciphertext", sa.LargeBinary, nullable=False),
        sa.Column(
            "event_types", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("disabled_reason", sa.String(200), nullable=True),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("org_id", "url", name="uq_webhook_org_url"),
    )
    op.create_index("ix_webhook_endpoints_org_id", "webhook_endpoints", ["org_id"])

    op.create_table(
        "outbox_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "org_id",
            UUID,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("resource_type", sa.String(60), nullable=False),
        sa.Column("resource_id", sa.String(120), nullable=False),
        # Identifiers and non-identifying metadata only. Enforced in code by an
        # allowlist (outbox/events.py) because this payload leaves the network.
        sa.Column(
            "payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("event_key", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="8"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(80), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("last_status_code", sa.Integer, nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # The constraint IS the deduplication. Two workers racing to report the
        # same domain event must produce one row, and application logic cannot
        # promise that.
        sa.UniqueConstraint("event_key", name="uq_outbox_event_key"),
    )
    op.create_index("ix_outbox_events_org_id", "outbox_events", ["org_id"])
    # Partial: the relay scans for deliverable work almost exclusively, and the
    # table is mostly delivered rows.
    op.create_index(
        "ix_outbox_deliverable",
        "outbox_events",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON outbox_events, webhook_endpoints TO screener_app"
    )


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_table("webhook_endpoints")

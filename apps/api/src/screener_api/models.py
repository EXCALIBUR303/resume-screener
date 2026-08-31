"""Database models for M1: tenancy, identity, sessions, and the audit chain."""

from __future__ import annotations

import datetime as dt
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=180)
    created_at: Mapped[dt.datetime] = _now()

    # passive_deletes=True is load-bearing, not tidiness: without it SQLAlchemy
    # "de-associates" children by setting org_id = NULL instead of letting the
    # FK's ON DELETE CASCADE fire. On the erasure path that orphans rows rather
    # than removing them — the exact residue AC-14 forbids.
    users: Mapped[list[User]] = relationship(
        back_populates="organization", cascade="all, delete-orphan", passive_deletes=True
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_users_org_email"),)

    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # Null for OAuth-only accounts. Never populated for a candidate — candidates
    # are not users and have no login.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_subject: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = _now()

    organization: Mapped[Organization] = relationship(back_populates="users")
    roles: Mapped[list[UserRole]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
    )

    @property
    def role_names(self) -> frozenset[str]:
        return frozenset(r.role for r in self.roles)


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role", name="uq_user_roles"),)

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    granted_at: Mapped[dt.datetime] = _now()

    user: Mapped[User] = relationship(back_populates="roles")


class RefreshToken(Base):
    """Rotating refresh tokens with family-based reuse detection.

    A refresh token is single-use. Presenting one that has already been rotated
    means it was captured, so the entire family is revoked — the attacker and
    the legitimate user are both logged out, which is the correct outcome.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = _pk()
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Only the hash is stored: a database read must not yield usable tokens.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = _now()


class AuditEvent(Base):
    """Append-only, hash-chained audit log.

    ``hash = sha256(prev_hash || canonical_json(payload))``. Editing or deleting
    any row breaks every subsequent link, which ``make verify-audit`` detects.
    The app database role is denied UPDATE and DELETE on this table by migration,
    so tamper-evidence is enforced by the database, not by convention.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_org_created", "org_id", "created_at"),
        Index("ix_audit_resource", "resource_type", "resource_id"),
    )

    # Monotonic sequence defines chain order; UUIDs would not.
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4, unique=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(60), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="success")
    # Never raw content — hashes and non-identifying metadata only, so that a
    # candidate erasure can leave the chain intact (see C.3 in the blueprint).
    meta: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = _now()


class StoredFile(Base):
    """An uploaded blob. Content-addressed; the user's filename is a label only."""

    __tablename__ = "files"
    __table_args__ = (
        # Deduplication is per tenant: two orgs uploading the same resume get
        # separate rows, so deleting one org's copy cannot affect the other.
        UniqueConstraint("org_id", "sha256", name="uq_files_org_sha"),
    )

    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(140), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_declared: Mapped[str | None] = mapped_column(String(180), nullable=True)
    mime_sniffed: Mapped[str] = mapped_column(String(180), nullable=False)
    mime_resolved: Mapped[str] = mapped_column(String(180), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scan_status: Mapped[str] = mapped_column(String(20), nullable=False, default="skipped")
    scan_engine: Mapped[str | None] = mapped_column(String(60), nullable=True)
    is_quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[dt.datetime] = _now()


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # A stable, non-identifying handle shown in the UI until PII is re-hydrated.
    pseudonym: Mapped[str] = mapped_column(String(60), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[dt.datetime] = _now()
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    resumes: Mapped[list[Resume]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan", passive_deletes=True
    )


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="RESTRICT"), nullable=False
    )
    # Provenance travels with the row: a score is never interpretable without it.
    parse_status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    parse_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ocr_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ocr_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language: Mapped[str | None] = mapped_column(String(12), nullable=True)
    pipeline_version: Mapped[str] = mapped_column(String(20), nullable=False, default="0")
    redacted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    needs_manual_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = _now()

    candidate: Mapped[Candidate] = relationship(back_populates="resumes")


class JobQueue(Base):
    """Postgres-backed work queue, consumed with FOR UPDATE SKIP LOCKED.

    The idempotency key is a UNIQUE constraint rather than application logic
    (rule D-6): enqueueing the same work twice is a no-op enforced by the
    database, and re-running a job produces an identical row.
    """

    __tablename__ = "job_queue"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_job_idempotency"),
        # Partial index: the queue is scanned almost exclusively for pending work,
        # and the table is mostly completed rows.
        Index(
            "ix_job_queue_claimable",
            "job_type",
            "run_after",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_job_queue_lease", "status", "locked_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    job_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    run_after: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Retryable vs terminal is an explicit classification, not a guess at the
    # call site: terminal failures skip retries entirely and go straight to the
    # dead-letter state.
    error_class: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[dt.datetime] = _now()
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResumeText(Base):
    """Extracted text for one resume.

    Redacted text is a separate column from the raw extraction: only the
    redacted form is allowed to reach embeddings, prompts, logs, or the search
    index (M4 fills it). Keeping them apart at the schema level makes the wrong
    one hard to use by accident.
    """

    __tablename__ = "resume_texts"
    __table_args__ = (UniqueConstraint("resume_id", name="uq_resume_text"),)

    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resume_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    text_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sections: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    extractor: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[dt.datetime] = _now()


class PiiMap(Base):
    """The encrypted token -> original mapping for one resume.

    Held apart from the redacted text on purpose. The model path reads
    ``resume_texts.text_redacted`` and has no reason ever to touch this table;
    only the API process, serving an authorised human, decrypts it.
    """

    __tablename__ = "pii_maps"
    __table_args__ = (UniqueConstraint("resume_id", name="uq_pii_map_resume"),)

    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resume_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # AES-256-GCM envelope, org_id as AAD. Useless without APP_KEK.
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    entity_counts: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = _now()


class ResumeChunk(Base):
    """One retrievable slice of a redacted resume.

    Nothing in this table has ever seen raw PII, which is what makes it safe to
    index and search. ``char_start``/``char_end`` point into
    ``resume_texts.text_redacted`` and must stay exact — M6 verifies quoted
    evidence against them.
    """

    __tablename__ = "resume_chunks"
    __table_args__ = (UniqueConstraint("resume_id", "chunk_index", name="uq_chunk_per_resume"),)

    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resume_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text_redacted: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    section: Mapped[str | None] = mapped_column(String(40), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)
    created_at: Mapped[dt.datetime] = _now()

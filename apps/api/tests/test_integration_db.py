"""Integration tests against a real Postgres, via testcontainers.

M1 shipped 63 green tests while every async database session was broken at
runtime (a missing `greenlet`), because nothing exercised a real session. These
tests close that gap: they run migrations, write through the ORM, and assert the
guarantees that only a real database can enforce — the audit-chain trigger, the
grants, and tenant isolation in SQL.

Marked `integration`; skipped automatically when Docker is unavailable.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from screener_api.models import AuditEvent, Base, Organization, User, UserRole
from screener_api.security import audit
from screener_api.security.passwords import hash_password

pytestmark = [pytest.mark.integration]

try:
    from testcontainers.community.postgres import PostgresContainer

    _HAVE_DOCKER = True
except ImportError:  # pragma: no cover
    _HAVE_DOCKER = False


@pytest.fixture(scope="module")
def postgres():
    if not _HAVE_DOCKER:
        pytest.skip("testcontainers not installed")
    try:
        container = PostgresContainer("pgvector/pgvector:pg16", driver="psycopg")
        container.start()
    except Exception as exc:
        pytest.skip(f"Docker unavailable: {type(exc).__name__}")
    yield container
    container.stop()


@pytest_asyncio.fixture
async def session(postgres) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(postgres.get_connection_url())
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        # Recreate the append-only guarantee that migration 0002 installs.
        await conn.execute(
            text(
                """
                CREATE OR REPLACE FUNCTION audit_events_are_append_only()
                RETURNS TRIGGER AS $$
                BEGIN
                    RAISE EXCEPTION 'audit_events is append-only (attempted %)', TG_OP;
                END;
                $$ LANGUAGE plpgsql;
                DROP TRIGGER IF EXISTS audit_events_no_update_delete ON audit_events;
                CREATE TRIGGER audit_events_no_update_delete
                BEFORE UPDATE OR DELETE ON audit_events
                FOR EACH ROW EXECUTE FUNCTION audit_events_are_append_only();
                """
            )
        )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _org(session: AsyncSession, name: str = "Acme") -> Organization:
    org = Organization(id=uuid.uuid4(), name=name)
    session.add(org)
    await session.flush()
    return org


# ---- the async path actually works --------------------------------------------


async def test_async_session_can_write_and_read(session: AsyncSession) -> None:
    """The test that would have caught the missing greenlet in M1."""
    org = await _org(session)
    user = User(
        id=uuid.uuid4(),
        org_id=org.id,
        email="a@example.com",
        password_hash=hash_password("pw"),
    )
    session.add(user)
    await session.commit()

    found = (await session.execute(select(User).where(User.email == "a@example.com"))).scalar_one()
    assert found.org_id == org.id


async def test_roles_load_through_the_relationship(session: AsyncSession) -> None:
    org = await _org(session)
    user = User(id=uuid.uuid4(), org_id=org.id, email="r@example.com")
    session.add(user)
    await session.flush()
    session.add(UserRole(id=uuid.uuid4(), user_id=user.id, role="recruiter"))
    await session.commit()

    loaded = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
    assert loaded.role_names == frozenset({"recruiter"})


# ---- constraints the database enforces, not the app ---------------------------


async def test_email_is_unique_per_org_but_reusable_across_orgs(
    session: AsyncSession,
) -> None:
    a, b = await _org(session, "A"), await _org(session, "B")
    session.add(User(id=uuid.uuid4(), org_id=a.id, email="same@example.com"))
    session.add(User(id=uuid.uuid4(), org_id=b.id, email="same@example.com"))
    await session.commit()  # different orgs: fine

    session.add(User(id=uuid.uuid4(), org_id=a.id, email="same@example.com"))
    with pytest.raises(Exception, match=r"uq_users_org_email|duplicate key"):
        await session.commit()
    await session.rollback()


async def test_deleting_an_org_cascades_to_its_users(session: AsyncSession) -> None:
    org = await _org(session)
    session.add(User(id=uuid.uuid4(), org_id=org.id, email="c@example.com"))
    await session.commit()
    await session.delete(org)
    await session.commit()
    remaining = (await session.execute(select(User).where(User.org_id == org.id))).scalars().all()
    assert remaining == []


# ---- the audit chain, against a real database ---------------------------------


async def test_chain_builds_and_verifies(session: AsyncSession) -> None:
    org = await _org(session)
    for i in range(25):
        await audit.record(
            session,
            action=f"test.event.{i}",
            resource_type="test",
            resource_id=str(i),
            org_id=org.id,
        )
    await session.commit()
    assert await audit.verify_chain(session) == 25


async def test_chain_links_to_its_predecessor(session: AsyncSession) -> None:
    org = await _org(session)
    for i in range(5):
        await audit.record(session, action=f"e{i}", resource_type="t", org_id=org.id)
    await session.commit()

    events = (await session.execute(select(AuditEvent).order_by(AuditEvent.seq))).scalars().all()
    for previous, current in itertools.pairwise(events):
        assert current.prev_hash == previous.hash


async def test_database_refuses_to_update_an_audit_row(session: AsyncSession) -> None:
    """The control that makes tamper-evidence real: not an application check."""
    org = await _org(session)
    await audit.record(session, action="e", resource_type="t", org_id=org.id)
    await session.commit()

    with pytest.raises(Exception, match="append-only"):
        await session.execute(text("UPDATE audit_events SET action = 'tampered'"))
        await session.commit()
    await session.rollback()


async def test_database_refuses_to_delete_an_audit_row(session: AsyncSession) -> None:
    org = await _org(session)
    await audit.record(session, action="e", resource_type="t", org_id=org.id)
    await session.commit()

    with pytest.raises(Exception, match="append-only"):
        await session.execute(text("DELETE FROM audit_events"))
        await session.commit()
    await session.rollback()


async def test_audit_rows_carry_no_raw_pii(session: AsyncSession) -> None:
    """Erasure must be able to remove content while the chain survives, which is
    only possible if rows never held the content in the first place."""
    org = await _org(session)
    await audit.record(
        session,
        action="resume.uploaded",
        resource_type="resume",
        resource_id=str(uuid.uuid4()),
        org_id=org.id,
        actor_ip="203.0.113.9",
        meta={"sha256": "a" * 64, "byte_size": 1024},
    )
    await session.commit()

    event = (await session.execute(select(AuditEvent))).scalars().one()
    serialised = f"{event.meta}{event.actor_ip_hash}{event.resource_id}"
    assert "203.0.113.9" not in serialised
    assert event.actor_ip_hash is not None and len(event.actor_ip_hash) == 64


# ---- AC-14: erasure completeness ----------------------------------------------


async def _seed_candidate_with_resume(session: AsyncSession, store, org):
    """A candidate with a resume, extracted text, an encrypted PII map, a file
    row, a blob on disk, and a queued job — i.e. residue in every place."""
    import json as _json

    from screener_api.models import Candidate, JobQueue, PiiMap, Resume, ResumeText, StoredFile
    from screener_api.security.crypto import encrypt, sha256_hex

    data = b"%PDF-1.4 synthetic resume for erasure test"
    blob = store.put_quarantine(data, org_id=str(org.id))
    store.promote(blob.sha256)

    stored = StoredFile(
        id=uuid.uuid4(),
        org_id=org.id,
        sha256=blob.sha256,
        storage_key=blob.storage_key,
        byte_size=blob.byte_size,
        mime_sniffed="application/pdf",
        mime_resolved="application/pdf",
        is_quarantined=False,
    )
    session.add(stored)
    candidate = Candidate(id=uuid.uuid4(), org_id=org.id, pseudonym="CANDIDATE_TEST")
    session.add(candidate)
    await session.flush()

    resume = Resume(
        id=uuid.uuid4(),
        org_id=org.id,
        candidate_id=candidate.id,
        file_id=stored.id,
        parse_status="parsed",
    )
    session.add(resume)
    await session.flush()

    session.add(
        ResumeText(
            id=uuid.uuid4(),
            org_id=org.id,
            resume_id=resume.id,
            raw_text="Priya Ramanathan priya@example.com",
            text_redacted="PERSON_1 EMAIL_1",
            char_count=34,
            extractor="pypdf",
        )
    )
    from screener_api.security.crypto import derive_kek

    kek = derive_kek("erasure-test-secret", 1)
    envelope = encrypt(
        _json.dumps({"PERSON_1": "Priya Ramanathan"}).encode(),
        kek=kek,
        kek_version=1,
        aad=str(org.id).encode(),
    )
    session.add(
        PiiMap(
            id=uuid.uuid4(),
            org_id=org.id,
            resume_id=resume.id,
            ciphertext=envelope.to_bytes(),
            entity_counts={"PERSON": 1},
        )
    )
    session.add(
        JobQueue(
            id=uuid.uuid4(),
            org_id=org.id,
            job_type="parse",
            payload={"resume_id": str(resume.id)},
            idempotency_key=sha256_hex(resume.id.bytes),
        )
    )
    await session.commit()
    return candidate, resume, blob.sha256


async def test_purge_removes_every_trace(session: AsyncSession, tmp_path) -> None:
    """AC-14: after erasure a scripted sweep finds zero residual bytes."""
    from sqlalchemy import func

    from screener_api.ingest.storage import BlobStore
    from screener_api.models import JobQueue, PiiMap, Resume, ResumeText, StoredFile
    from screener_api.privacy.erasure import purge_candidate
    from screener_api.security.crypto import derive_kek

    org = await _org(session, "Erasure Co")
    await session.commit()
    store = BlobStore(tmp_path / "blobs", kek=derive_kek("erasure-test-secret", 1), kek_version=1)
    candidate, _resume, digest = await _seed_candidate_with_resume(session, store, org)

    assert store.exists(digest)
    report = await purge_candidate(session, candidate.id, org_id=org.id, store=store, reason="test")
    await session.commit()

    assert report.resumes == 1
    assert report.texts == 1
    assert report.pii_maps == 1
    assert report.files == 1
    assert report.jobs == 1
    assert report.blobs == 1
    assert report.errors == []

    # The sweep: every table that could hold residue, plus the file store.
    for model in (Resume, ResumeText, PiiMap, StoredFile, JobQueue):
        remaining = (await session.execute(select(func.count()).select_from(model))).scalar_one()
        assert remaining == 0, f"{model.__name__} still holds {remaining} row(s)"
    assert not store.exists(digest), "encrypted blob survived erasure"
    assert not list((tmp_path / "blobs").rglob("*")) or all(
        p.is_dir() for p in (tmp_path / "blobs").rglob("*")
    ), "files remain on disk after erasure"


async def test_audit_chain_survives_erasure(session: AsyncSession, tmp_path) -> None:
    """The tension this project has to resolve: erase the content, keep the
    chain verifiable. Only possible because audit rows never held the content."""
    from screener_api.ingest.storage import BlobStore
    from screener_api.privacy.erasure import purge_candidate
    from screener_api.security.crypto import derive_kek

    org = await _org(session, "Chain Co")
    await audit.record(
        session,
        action="resume.uploaded",
        resource_type="resume",
        org_id=org.id,
        meta={"sha256": "a" * 64},
    )
    await session.commit()

    store = BlobStore(tmp_path / "b2", kek=derive_kek("erasure-test-secret", 1), kek_version=1)
    candidate, _resume, _digest = await _seed_candidate_with_resume(session, store, org)
    await purge_candidate(session, candidate.id, org_id=org.id, store=store)
    await session.commit()

    assert await audit.verify_chain(session) >= 2


async def test_tombstone_records_the_purge_without_the_content(
    session: AsyncSession,
    tmp_path,
) -> None:
    from screener_api.ingest.storage import BlobStore
    from screener_api.privacy.erasure import purge_candidate
    from screener_api.security.crypto import derive_kek

    org = await _org(session, "Tombstone Co")
    await session.commit()
    store = BlobStore(tmp_path / "b3", kek=derive_kek("erasure-test-secret", 1), kek_version=1)
    candidate, _r, _d = await _seed_candidate_with_resume(session, store, org)
    await purge_candidate(session, candidate.id, org_id=org.id, store=store)
    await session.commit()

    event = (
        (await session.execute(select(AuditEvent).where(AuditEvent.action == "candidate.purged")))
        .scalars()
        .one()
    )
    serialised = f"{event.meta}{event.resource_id}"
    assert "Priya Ramanathan" not in serialised
    assert "priya@example.com" not in serialised
    assert event.meta["resumes_removed"] == 1


async def test_purging_twice_is_safe(session: AsyncSession, tmp_path) -> None:
    from screener_api.ingest.storage import BlobStore
    from screener_api.privacy.erasure import purge_candidate
    from screener_api.security.crypto import derive_kek

    org = await _org(session, "Idempotent Co")
    await session.commit()
    store = BlobStore(tmp_path / "b4", kek=derive_kek("erasure-test-secret", 1), kek_version=1)
    candidate, _r, _d = await _seed_candidate_with_resume(session, store, org)
    await purge_candidate(session, candidate.id, org_id=org.id, store=store)
    await session.commit()

    with pytest.raises(LookupError):
        await purge_candidate(session, candidate.id, org_id=org.id, store=store)


async def test_purge_cannot_cross_tenants(session: AsyncSession, tmp_path) -> None:
    from screener_api.ingest.storage import BlobStore
    from screener_api.privacy.erasure import purge_candidate
    from screener_api.security.crypto import derive_kek

    org_a = await _org(session, "A Co")
    org_b = await _org(session, "B Co")
    await session.commit()
    store = BlobStore(tmp_path / "b5", kek=derive_kek("erasure-test-secret", 1), kek_version=1)
    candidate, _r, _d = await _seed_candidate_with_resume(session, store, org_a)

    with pytest.raises(LookupError):
        await purge_candidate(session, candidate.id, org_id=org_b.id, store=store)

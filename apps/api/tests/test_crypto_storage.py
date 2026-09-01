"""Envelope encryption and the content-addressed blob store."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from screener_api.ingest.storage import BlobStore, StorageError
from screener_api.security.crypto import (
    DecryptionError,
    Envelope,
    decrypt,
    derive_kek,
    encrypt,
    sha256_hex,
)

KEK_A = derive_kek("secret-value-a", 1)
KEK_B = derive_kek("secret-value-b", 1)


def test_roundtrip() -> None:
    data = b"Synthetic resume. SYNTHETIC-DATA-DO-NOT-USE"
    env = encrypt(data, kek=KEK_A, kek_version=1, aad=b"org-1")
    assert decrypt(env, kek=KEK_A, aad=b"org-1") == data


def test_plaintext_is_not_present_in_the_ciphertext() -> None:
    data = b"Priya Ramanathan priya@example.com"
    blob = encrypt(data, kek=KEK_A, kek_version=1).to_bytes()
    assert b"Priya" not in blob
    assert b"example.com" not in blob


def test_each_encryption_uses_a_fresh_data_key() -> None:
    a = encrypt(b"same", kek=KEK_A, kek_version=1)
    b = encrypt(b"same", kek=KEK_A, kek_version=1)
    assert a.ciphertext != b.ciphertext
    assert a.wrapped_dek != b.wrapped_dek


def test_wrong_kek_fails() -> None:
    env = encrypt(b"data", kek=KEK_A, kek_version=1)
    with pytest.raises(DecryptionError):
        decrypt(env, kek=KEK_B)


def test_wrong_aad_fails() -> None:
    """AAD binds ciphertext to its tenant: a blob moved between organisations
    fails authentication rather than decrypting."""
    env = encrypt(b"data", kek=KEK_A, kek_version=1, aad=b"org-1")
    with pytest.raises(DecryptionError):
        decrypt(env, kek=KEK_A, aad=b"org-2")


@pytest.mark.parametrize("position", [0, 20, -1])
def test_tampering_is_detected(position: int) -> None:
    """GCM is authenticated: altered bytes fail rather than decrypting to garbage."""
    env = encrypt(b"x" * 200, kek=KEK_A, kek_version=1)
    raw = bytearray(env.to_bytes())
    raw[position] ^= 0xFF
    with pytest.raises(DecryptionError):
        decrypt(Envelope.from_bytes(bytes(raw)), kek=KEK_A)


def test_envelope_serialisation_roundtrip() -> None:
    env = encrypt(b"payload", kek=KEK_A, kek_version=7)
    restored = Envelope.from_bytes(env.to_bytes())
    assert restored.kek_version == 7
    assert decrypt(restored, kek=KEK_A) == b"payload"


def test_unrecognised_envelope_is_rejected() -> None:
    with pytest.raises(DecryptionError):
        Envelope.from_bytes(b"not an envelope at all")


def test_kek_versions_derive_different_keys() -> None:
    """Rotation must actually change the key, or it is theatre."""
    assert derive_kek("same-secret", 1) != derive_kek("same-secret", 2)


# ---- blob store ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "files", kek=KEK_A, kek_version=1)


def test_quarantine_then_promote(store: BlobStore) -> None:
    data = b"%PDF-1.4 synthetic"
    blob = store.put_quarantine(data, org_id="org-1")
    assert not store.exists(blob.sha256), "must not be readable before promotion"
    store.promote(blob.sha256)
    assert store.exists(blob.sha256)
    assert store.get(blob.sha256, org_id="org-1") == data


def test_unpromoted_blob_cannot_be_read(store: BlobStore) -> None:
    blob = store.put_quarantine(b"data", org_id="org-1")
    with pytest.raises(StorageError):
        store.get(blob.sha256, org_id="org-1")


def test_another_org_cannot_decrypt(store: BlobStore) -> None:
    blob = store.put_quarantine(b"confidential", org_id="org-1")
    store.promote(blob.sha256)
    with pytest.raises(DecryptionError):
        store.get(blob.sha256, org_id="org-2")


def test_storage_key_is_the_content_address(store: BlobStore) -> None:
    data = b"deterministic"
    blob = store.put_quarantine(data, org_id="org-1")
    assert blob.sha256 == sha256_hex(data)
    assert blob.storage_key == f"{blob.sha256[:2]}/{blob.sha256[2:4]}/{blob.sha256}"


def test_identical_content_maps_to_one_key(store: BlobStore) -> None:
    a = store.put_quarantine(b"same bytes", org_id="org-1")
    b = store.put_quarantine(b"same bytes", org_id="org-1")
    assert a.sha256 == b.sha256


@pytest.mark.parametrize(
    "bad",
    ["../../etc/passwd", "..", "/etc/passwd", "", "not-hex", "ab/cd", "A" * 64, "0" * 63],
)
def test_only_a_real_digest_can_become_a_path(store: BlobStore, bad: str) -> None:
    """The store accepts nothing but a 64-char lowercase hex digest, so no
    user-controlled string can ever become a path component."""
    with pytest.raises(StorageError):
        BlobStore.key_for(bad)


def test_nothing_from_the_user_reaches_disk(store: BlobStore, tmp_path: Path) -> None:
    store.promote(store.put_quarantine(b"payload", org_id="org-1").sha256)
    names = {p.name for p in (tmp_path / "files").rglob("*") if p.is_file()}
    for name in names:
        assert all(c in "0123456789abcdef" for c in name), f"non-hex filename on disk: {name}"


def test_delete_removes_all_copies(store: BlobStore) -> None:
    blob = store.put_quarantine(b"erase me", org_id="org-1")
    store.promote(blob.sha256)
    store.delete(blob.sha256)
    assert not store.exists(blob.sha256)
    with pytest.raises(StorageError):
        store.get(blob.sha256, org_id="org-1")


def test_discard_removes_a_quarantined_blob(store: BlobStore) -> None:
    blob = store.put_quarantine(b"rejected", org_id="org-1")
    store.discard(blob.sha256)
    with pytest.raises(StorageError):
        store.promote(blob.sha256)


def test_no_partial_files_are_left_behind(store: BlobStore, tmp_path: Path) -> None:
    """Writes are atomic: a reader never sees a half-written blob."""
    store.put_quarantine(b"data", org_id="org-1")
    assert not list((tmp_path / "files").rglob("*.part"))


# ---- S3 backend ---------------------------------------------------------------


class FakeS3:
    """An in-memory stand-in for S3. Enough of the API to exercise the store.

    A fake rather than moto: the point is to test OUR logic — key derivation,
    encryption placement, promotion semantics — not to re-test boto3.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:  # noqa: N803
        self.objects[f"{Bucket}/{Key}"] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        import io

        data = self.objects[f"{Bucket}/{Key}"]
        return {"Body": io.BytesIO(data)}

    def head_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        if f"{Bucket}/{Key}" not in self.objects:
            raise KeyError(Key)
        return {}

    def copy_object(self, *, Bucket: str, CopySource: dict, Key: str) -> None:  # noqa: N803
        self.objects[f"{Bucket}/{Key}"] = self.objects[
            f"{CopySource['Bucket']}/{CopySource['Key']}"
        ]

    def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
        self.objects.pop(f"{Bucket}/{Key}", None)


@pytest.fixture
def s3_store():
    from screener_api.ingest.storage import S3BlobStore

    fake = FakeS3()
    store = S3BlobStore("bucket", kek=KEK_A, kek_version=1, client=fake)
    return store, fake


def test_s3_quarantine_then_promote(s3_store) -> None:
    store, _ = s3_store
    data = b"%PDF-1.4 synthetic"
    blob = store.put_quarantine(data, org_id="org-1")
    assert not store.exists(blob.sha256), "must not be readable before promotion"
    store.promote(blob.sha256)
    assert store.exists(blob.sha256)
    assert store.get(blob.sha256, org_id="org-1") == data


def test_s3_bucket_only_ever_holds_ciphertext(s3_store) -> None:
    """The provider must never see plaintext. A misconfigured bucket policy then
    leaks ciphertext rather than resumes."""
    store, fake = s3_store
    store.put_quarantine(b"Priya Ramanathan priya@example.com", org_id="org-1")
    for body in fake.objects.values():
        assert b"Priya" not in body
        assert b"example.com" not in body


def test_s3_another_org_cannot_decrypt(s3_store) -> None:
    store, _ = s3_store
    blob = store.put_quarantine(b"confidential", org_id="org-1")
    store.promote(blob.sha256)
    with pytest.raises(DecryptionError):
        store.get(blob.sha256, org_id="org-2")


def test_s3_keys_are_content_addressed(s3_store) -> None:
    store, fake = s3_store
    blob = store.put_quarantine(b"deterministic", org_id="org-1")
    key = next(iter(fake.objects))
    assert blob.sha256 in key
    assert key.startswith(f"bucket/quarantine/{blob.sha256[:2]}/")


def test_s3_promotion_removes_the_quarantine_copy(s3_store) -> None:
    store, fake = s3_store
    blob = store.put_quarantine(b"data", org_id="org-1")
    store.promote(blob.sha256)
    assert not any("quarantine" in k for k in fake.objects)


def test_s3_delete_is_idempotent(s3_store) -> None:
    """Erasure must be repeatable, or a partially-completed purge can never be
    finished."""
    store, _ = s3_store
    blob = store.put_quarantine(b"erase me", org_id="org-1")
    store.promote(blob.sha256)
    store.delete(blob.sha256)
    store.delete(blob.sha256)
    assert not store.exists(blob.sha256)


def test_s3_rejects_a_non_digest_key(s3_store) -> None:
    from screener_api.ingest.storage import S3BlobStore

    _store, _ = s3_store
    for bad in ("../../etc/passwd", "not-hex", ""):
        with pytest.raises(StorageError):
            S3BlobStore._key("clean", bad)


def test_both_stores_satisfy_the_protocol(tmp_path) -> None:
    """The pipeline depends on the protocol, not on either implementation."""
    from screener_api.ingest.storage import ObjectStore, S3BlobStore

    local = BlobStore(tmp_path / "f", kek=KEK_A, kek_version=1)
    remote = S3BlobStore("b", kek=KEK_A, kek_version=1, client=FakeS3())
    assert isinstance(local, ObjectStore)
    assert isinstance(remote, ObjectStore)


# ---- build_store: the one place storage_backend is read ----------------------
#
# Before this factory existed, worker.py and resumes.py each hardcoded
# BlobStore directly, so STORAGE_BACKEND=s3 changed nothing: cloud mode
# silently wrote resumes to a container's ephemeral local disk instead of the
# configured bucket, and S3BlobStore -- tested, encrypted, content-addressed
# the same way as the local store -- sat beside the pipeline, unreachable by
# any real request. Found writing the cloud deployment walkthrough, not by a
# report.


@dataclass
class _FakeSecret:
    value: str

    def get_secret_value(self) -> str:
        return self.value


@dataclass
class _FakeSettings:
    storage_backend: str
    # Unused by the s3-backend tests; local-backend tests always override it.
    storage_local_path: Path = field(default_factory=lambda: Path())
    s3_bucket: str = ""
    s3_endpoint_url: str = ""
    s3_access_key_id: _FakeSecret = field(default_factory=lambda: _FakeSecret(""))
    s3_secret_access_key: _FakeSecret = field(default_factory=lambda: _FakeSecret(""))


def test_build_store_returns_the_local_store_by_default(tmp_path: Path) -> None:
    from screener_api.ingest.storage import BlobStore, build_store

    settings = _FakeSettings(storage_backend="local", storage_local_path=tmp_path)
    store = build_store(settings, kek=KEK_A, kek_version=1)
    assert isinstance(store, BlobStore)


def test_build_store_returns_the_s3_store_when_configured(monkeypatch) -> None:
    from screener_api.ingest.storage import S3BlobStore, build_store

    captured: dict[str, object] = {}

    def fake_client(service: str, **kwargs: object) -> object:
        captured["service"] = service
        captured["kwargs"] = kwargs
        return FakeS3()

    monkeypatch.setattr("boto3.client", fake_client)

    settings = _FakeSettings(
        storage_backend="s3",
        s3_bucket="prod-bucket",
        s3_endpoint_url="https://example.r2.cloudflarestorage.com",
        s3_access_key_id=_FakeSecret("AKIA_TEST"),
        s3_secret_access_key=_FakeSecret("shh"),
    )
    store = build_store(settings, kek=KEK_A, kek_version=1)

    assert isinstance(store, S3BlobStore)
    assert store.bucket == "prod-bucket"
    # The credentials reached boto3 BY NAME, not through its ambient AWS_*
    # environment-variable chain -- this project's settings are named
    # S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY, and silently also honouring the
    # AWS_* names would mean two names work and only one is documented, which
    # is how a credential ends up unset in production.
    assert captured["kwargs"] == {
        "endpoint_url": "https://example.r2.cloudflarestorage.com",
        "aws_access_key_id": "AKIA_TEST",
        "aws_secret_access_key": "shh",
    }


def test_build_store_omits_credentials_it_was_not_given(monkeypatch) -> None:
    """An s3 backend with no keys configured should fall through to boto3's
    own credential resolution rather than pass explicit empty strings, which
    botocore treats as real (and wrong) credentials rather than "unset"."""
    from screener_api.ingest.storage import build_store

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "boto3.client",
        lambda service, **kwargs: captured.update(kwargs) or FakeS3(),
    )

    settings = _FakeSettings(storage_backend="s3", s3_bucket="b")
    build_store(settings, kek=KEK_A, kek_version=1)

    assert "aws_access_key_id" not in captured
    assert "aws_secret_access_key" not in captured
    assert "endpoint_url" not in captured


def test_every_store_call_site_goes_through_the_factory() -> None:
    """A guard against the exact regression this factory fixes.

    Verified to actually fire before trusting it: I broke each of these by
    hand (reverted worker.py and resumes.py to construct BlobStore directly)
    and confirmed this test failed both times before restoring them. A test
    that cannot fail is the same defect as the config it is guarding against.
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "screener_api"
    offenders = []
    for path in (src / "worker.py", src / "routers" / "resumes.py"):
        text = path.read_text()
        if "build_store(" not in text:
            offenders.append(path.name)
        if "= BlobStore(" in text or "return BlobStore(" in text:
            offenders.append(f"{path.name} constructs BlobStore directly")

    assert not offenders, f"storage_backend is bypassed in: {', '.join(offenders)}"

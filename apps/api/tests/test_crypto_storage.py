"""Envelope encryption and the content-addressed blob store."""

from __future__ import annotations

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

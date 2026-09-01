"""Content-addressed, encrypted blob store.

Two properties that matter:

1. **The user's filename never reaches the filesystem.** The on-disk key is
   derived from the SHA-256 of the contents, sharded two levels. Path traversal
   is impossible because no attacker-controlled string is ever a path component.
2. **Quarantine before clean.** Bytes land in ``quarantine/`` and are promoted to
   ``clean/`` only after validation (and a scan, when enabled). Nothing reads
   from ``clean/`` that has not passed.
"""

from __future__ import annotations

import contextlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from screener_api.security.crypto import Envelope, decrypt, encrypt, sha256_hex

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class StorageError(Exception):
    pass


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    storage_key: str
    byte_size: int


@runtime_checkable
class ObjectStore(Protocol):
    """What the pipeline needs from a blob store, and nothing more.

    Two implementations: local disk for development, S3-compatible for cloud
    hosts whose container filesystems are ephemeral. Encryption happens in this
    layer either way — the bucket never sees plaintext, so a misconfigured
    bucket policy leaks ciphertext rather than resumes.
    """

    def put_quarantine(self, data: bytes, *, org_id: str) -> StoredBlob: ...
    def promote(self, digest: str) -> None: ...
    def discard(self, digest: str) -> None: ...
    def exists(self, digest: str) -> bool: ...
    def get(self, digest: str, *, org_id: str) -> bytes: ...
    def delete(self, digest: str) -> None: ...


class BlobStore:
    def __init__(self, root: Path, *, kek: bytes, kek_version: int) -> None:
        self.root = Path(root)
        self.quarantine = self.root / "quarantine"
        self.clean = self.root / "clean"
        for directory in (self.quarantine, self.clean):
            directory.mkdir(parents=True, exist_ok=True)
        self._kek = kek
        self._kek_version = kek_version

    @staticmethod
    def key_for(digest: str) -> str:
        """Sharded content address: ab/cd/abcd… — never a user-supplied name."""
        if not _HEX64.match(digest):
            raise StorageError(f"not a sha256 digest: {digest[:32]!r}")
        return f"{digest[:2]}/{digest[2:4]}/{digest}"

    def _path(self, base: Path, digest: str) -> Path:
        path = (base / self.key_for(digest)).resolve()
        # Belt and braces: even though the key is derived from a validated hex
        # digest, confirm the resolved path stays inside the store.
        if not str(path).startswith(str(base.resolve())):
            raise StorageError("resolved path escaped the store root")
        return path

    def put_quarantine(self, data: bytes, *, org_id: str) -> StoredBlob:
        digest = sha256_hex(data)
        path = self._path(self.quarantine, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        # org_id as AAD binds the ciphertext to its tenant: a blob moved between
        # organisations fails authentication rather than decrypting.
        envelope = encrypt(data, kek=self._kek, kek_version=self._kek_version, aad=org_id.encode())
        tmp = path.with_suffix(".part")
        tmp.write_bytes(envelope.to_bytes())
        tmp.replace(path)  # atomic: a reader never sees a partial blob
        return StoredBlob(sha256=digest, storage_key=self.key_for(digest), byte_size=len(data))

    def promote(self, digest: str) -> None:
        source = self._path(self.quarantine, digest)
        if not source.exists():
            raise StorageError(f"nothing quarantined for {digest[:16]}…")
        target = self._path(self.clean, digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))

    def discard(self, digest: str) -> None:
        self._path(self.quarantine, digest).unlink(missing_ok=True)

    def exists(self, digest: str) -> bool:
        return self._path(self.clean, digest).exists()

    def get(self, digest: str, *, org_id: str) -> bytes:
        path = self._path(self.clean, digest)
        if not path.exists():
            raise StorageError(f"no clean blob for {digest[:16]}…")
        plaintext = decrypt(
            Envelope.from_bytes(path.read_bytes()), kek=self._kek, aad=org_id.encode()
        )
        if sha256_hex(plaintext) != digest:
            # Cannot normally happen: GCM would have failed first. A mismatch
            # means the store is corrupt, and serving the bytes anyway would be
            # worse than failing.
            raise StorageError("content address does not match decrypted bytes")
        return plaintext

    def delete(self, digest: str) -> None:
        """Hard delete, for the erasure path. Both locations, no tombstone file."""
        for base in (self.clean, self.quarantine):
            self._path(base, digest).unlink(missing_ok=True)


class S3BlobStore:
    """S3-compatible object storage, for hosts with ephemeral disks.

    The same content-addressed keys and the same envelope encryption as the
    local store: bytes are encrypted before they leave this process, so the
    provider holds ciphertext and nothing else. Quarantine and clean are key
    prefixes rather than directories, and promotion is a server-side copy.
    """

    def __init__(
        self,
        bucket: str,
        *,
        kek: bytes,
        kek_version: int,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self._kek = kek
        self._kek_version = kek_version
        if client is not None:
            self._s3 = client
        else:
            import boto3

            # Explicit credentials, not boto3's ambient AWS_* env var chain.
            # This project's own settings are named S3_ACCESS_KEY_ID and
            # S3_SECRET_ACCESS_KEY (see docs/deployment.md); silently also
            # accepting AWS_ACCESS_KEY_ID would mean two names work and only
            # one is documented, which is how a credential ends up unset in
            # production because it was named the way the docs said.
            kwargs: dict[str, str] = {}
            if endpoint_url:
                kwargs["endpoint_url"] = endpoint_url
            if access_key_id and secret_access_key:
                kwargs["aws_access_key_id"] = access_key_id
                kwargs["aws_secret_access_key"] = secret_access_key
            self._s3 = boto3.client("s3", **kwargs)

    @staticmethod
    def _key(prefix: str, digest: str) -> str:
        # Reuses the local store's validation, so a non-digest can never become
        # an object key any more than it can become a path.
        return f"{prefix}/{BlobStore.key_for(digest)}"

    def put_quarantine(self, data: bytes, *, org_id: str) -> StoredBlob:
        digest = sha256_hex(data)
        envelope = encrypt(data, kek=self._kek, kek_version=self._kek_version, aad=org_id.encode())
        self._s3.put_object(
            Bucket=self.bucket,
            Key=self._key("quarantine", digest),
            Body=envelope.to_bytes(),
        )
        return StoredBlob(sha256=digest, storage_key=BlobStore.key_for(digest), byte_size=len(data))

    def promote(self, digest: str) -> None:
        source = self._key("quarantine", digest)
        self._s3.copy_object(
            Bucket=self.bucket,
            CopySource={"Bucket": self.bucket, "Key": source},
            Key=self._key("clean", digest),
        )
        self._s3.delete_object(Bucket=self.bucket, Key=source)

    def discard(self, digest: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=self._key("quarantine", digest))

    def exists(self, digest: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=self._key("clean", digest))
        except Exception:
            return False
        return True

    def get(self, digest: str, *, org_id: str) -> bytes:
        try:
            body = self._s3.get_object(Bucket=self.bucket, Key=self._key("clean", digest))[
                "Body"
            ].read()
        except Exception as exc:
            raise StorageError(f"no clean object for {digest[:16]}…") from exc

        plaintext = decrypt(Envelope.from_bytes(body), kek=self._kek, aad=org_id.encode())
        if sha256_hex(plaintext) != digest:
            raise StorageError("content address does not match decrypted bytes")
        return plaintext

    def delete(self, digest: str) -> None:
        for prefix in ("clean", "quarantine"):
            # Deleting an absent object is success, not failure: erasure must
            # be idempotent or a partially-completed purge can never finish.
            with contextlib.suppress(Exception):
                self._s3.delete_object(Bucket=self.bucket, Key=self._key(prefix, digest))


def build_store(settings: Any, *, kek: bytes, kek_version: int) -> ObjectStore:
    """The one place `storage_backend` is read to choose an implementation.

    Before this existed, `worker.py` and `resumes.py` each constructed
    `BlobStore` directly, so `STORAGE_BACKEND=s3` changed nothing: cloud mode
    silently wrote resumes to a container's ephemeral local disk instead of
    the configured bucket, and `S3BlobStore` — tested, encrypted, content-
    addressed the same way as the local store — sat beside the pipeline
    unreachable by any real request. Found while writing the cloud deployment
    walkthrough, not by a report; nothing had ever exercised `storage_backend
    == "s3"` end to end.

    Typed as `Any` for `settings` rather than importing `Settings` here: that
    module has no reason to import this one back, and this keeps it that way.
    """
    if settings.storage_backend == "s3":
        return S3BlobStore(
            settings.s3_bucket,
            kek=kek,
            kek_version=kek_version,
            endpoint_url=settings.s3_endpoint_url or None,
            access_key_id=settings.s3_access_key_id.get_secret_value() or None,
            secret_access_key=settings.s3_secret_access_key.get_secret_value() or None,
        )
    return BlobStore(settings.storage_local_path, kek=kek, kek_version=kek_version)

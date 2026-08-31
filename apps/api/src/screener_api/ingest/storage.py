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

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from screener_api.security.crypto import Envelope, decrypt, encrypt, sha256_hex

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class StorageError(Exception):
    pass


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    storage_key: str
    byte_size: int


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

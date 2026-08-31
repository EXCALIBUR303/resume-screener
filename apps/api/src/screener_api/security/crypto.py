"""Envelope encryption for data at rest.

Each blob gets its own random data key (DEK). The DEK is wrapped with the
key-encryption key (KEK) from ``APP_KEK`` and stored alongside the ciphertext.
Rotating the KEK re-wraps DEKs without touching a byte of ciphertext, which is
what makes rotation feasible on a large store.

AES-256-GCM: authenticated, so tampering with stored bytes is detected on read
rather than silently decrypting to garbage.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32  # AES-256
NONCE_BYTES = 12  # GCM standard
_MAGIC = b"SCRN1"  # format marker, so a future format change is detectable


class DecryptionError(Exception):
    """Ciphertext failed authentication, or the wrong key was supplied."""


@dataclass(frozen=True)
class Envelope:
    """A wrapped data key plus the ciphertext it protects."""

    kek_version: int
    wrapped_dek: bytes
    ciphertext: bytes

    def to_bytes(self) -> bytes:
        return b"".join(
            [
                _MAGIC,
                self.kek_version.to_bytes(2, "big"),
                len(self.wrapped_dek).to_bytes(2, "big"),
                self.wrapped_dek,
                self.ciphertext,
            ]
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> Envelope:
        if not raw.startswith(_MAGIC):
            raise DecryptionError("not a recognised envelope")
        pos = len(_MAGIC)
        version = int.from_bytes(raw[pos : pos + 2], "big")
        pos += 2
        wrapped_len = int.from_bytes(raw[pos : pos + 2], "big")
        pos += 2
        wrapped = raw[pos : pos + wrapped_len]
        pos += wrapped_len
        return cls(kek_version=version, wrapped_dek=wrapped, ciphertext=raw[pos:])


def derive_kek(secret: str, version: int) -> bytes:
    """Derive a 32-byte KEK from the configured secret.

    Versioned: `make rotate-kek` bumps the version, derives a new KEK, and
    re-wraps every DEK. Old versions must remain resolvable until rotation
    completes, which is why the version travels inside the envelope.
    """
    return hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode(),
        f"screener-kek-v{version}".encode(),
        iterations=200_000,
        dklen=KEY_BYTES,
    )


def encrypt(
    plaintext: bytes, *, kek: bytes, kek_version: int, aad: bytes | None = None
) -> Envelope:
    dek = os.urandom(KEY_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = nonce + AESGCM(dek).encrypt(nonce, plaintext, aad)

    wrap_nonce = os.urandom(NONCE_BYTES)
    wrapped = wrap_nonce + AESGCM(kek).encrypt(wrap_nonce, dek, None)
    return Envelope(kek_version=kek_version, wrapped_dek=wrapped, ciphertext=ciphertext)


def decrypt(envelope: Envelope, *, kek: bytes, aad: bytes | None = None) -> bytes:
    try:
        dek = AESGCM(kek).decrypt(
            envelope.wrapped_dek[:NONCE_BYTES], envelope.wrapped_dek[NONCE_BYTES:], None
        )
        return AESGCM(dek).decrypt(
            envelope.ciphertext[:NONCE_BYTES], envelope.ciphertext[NONCE_BYTES:], aad
        )
    except InvalidTag as exc:
        raise DecryptionError("authentication failed: wrong key or altered ciphertext") from exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()

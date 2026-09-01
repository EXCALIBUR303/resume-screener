"""Webhook signatures.

``v1=HMAC-SHA256(secret, "<timestamp>.<body>")``, sent alongside the timestamp.

The timestamp is inside the signed material, not merely beside it. Signing the
body alone produces a token that is valid forever: anyone who captures one
request can replay it indefinitely and the signature still verifies. With the
timestamp signed, a receiver rejects anything outside a tolerance window and a
captured request expires.

Verification uses a constant-time compare. A byte-by-byte `==` on a MAC leaks
how many leading bytes were right, which is enough to forge one over enough
requests.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_VERSION = "v1"
# How old a signed request may be. Long enough to survive a slow receiver and a
# little clock skew; short enough that a captured request stops working.
DEFAULT_TOLERANCE_SECONDS = 300


def signed_payload(timestamp: int, body: bytes) -> bytes:
    return f"{timestamp}.".encode() + body


def sign(secret: bytes, *, timestamp: int, body: bytes) -> str:
    digest = hmac.new(secret, signed_payload(timestamp, body), hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def verify(
    secret: bytes,
    *,
    timestamp: int,
    body: bytes,
    signature: str,
    now: int,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """Reference implementation, for the docs and for the tests to prove.

    Shipping this means the documented verification is executable rather than a
    snippet in a README that has never run.
    """
    if abs(now - timestamp) > tolerance_seconds:
        return False
    return hmac.compare_digest(sign(secret, timestamp=timestamp, body=body), signature)

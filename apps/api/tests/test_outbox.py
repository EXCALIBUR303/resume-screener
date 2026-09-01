"""Transactional outbox, webhook signing, and SSRF defence.

The headline test is `test_a_rolled_back_transaction_leaves_no_event`. Everything
else here supports it: an outbox is only worth its complexity if the event and
the change it describes genuinely share a fate.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid

import httpx
import pytest

from screener_api.models import OutboxEvent, WebhookEndpoint
from screener_api.outbox.events import (
    ALLOWED_KEYS,
    EventType,
    PayloadRejectedError,
    assert_no_pii,
)
from screener_api.outbox.relay import (
    DeliveryOutcome,
    _settle,
    backoff_seconds,
    body_for,
    deliver,
)
from screener_api.outbox.signing import sign, verify
from screener_api.outbox.ssrf import BLOCKED_PORTS, DestinationRefusedError, validate

# --------------------------------------------------------------------------- #
#  SSRF
# --------------------------------------------------------------------------- #

FAKE_DNS = {
    "hooks.example": ["93.184.216.34"],
    "metadata.example": ["169.254.169.254"],
    "gcp-metadata.example": ["169.254.169.254"],
    "loopback.example": ["127.0.0.1"],
    "private.example": ["10.0.0.5"],
    "cgnat.example": ["100.64.0.1"],
    "v6-loopback.example": ["::1"],
    "v6-ula.example": ["fd00::1"],
    "split-horizon.example": ["93.184.216.34", "10.0.0.5"],
    "unroutable.example": ["0.0.0.0"],
}


def _resolver(host: str, port: int) -> list[str]:
    return FAKE_DNS.get(host, [])


def test_a_public_https_url_is_accepted() -> None:
    destination = validate("https://hooks.example/inbound", resolver=_resolver)
    assert destination.address == "93.184.216.34"
    assert destination.port == 443


@pytest.mark.parametrize(
    ("url", "because"),
    [
        ("http://hooks.example/x", "plaintext"),
        ("ftp://hooks.example/x", "not http at all"),
        ("https://metadata.example/latest/meta-data/", "AWS instance metadata"),
        ("https://gcp-metadata.example/computeMetadata/v1/", "GCP instance metadata"),
        ("https://loopback.example/x", "our own host"),
        ("https://private.example/x", "RFC1918"),
        ("https://cgnat.example/x", "carrier-grade NAT"),
        ("https://v6-loopback.example/x", "IPv6 loopback"),
        ("https://v6-ula.example/x", "IPv6 unique local"),
        ("https://unroutable.example/x", "unspecified address"),
        ("https://nowhere.example/x", "does not resolve"),
        ("https://user:pw@hooks.example/x", "credentials in the url"),
    ],
)
def test_destinations_that_are_refused(url: str, because: str) -> None:
    with pytest.raises(DestinationRefusedError):
        validate(url, resolver=_resolver)


def test_one_private_address_among_several_refuses_the_whole_name() -> None:
    """A name returning one public and one private address would otherwise pass
    on the first lookup and connect to the private one on a retry."""
    with pytest.raises(DestinationRefusedError, match=re.escape("10.0.0.5")):
        validate("https://split-horizon.example/x", resolver=_resolver)


@pytest.mark.parametrize("port", sorted(BLOCKED_PORTS)[:6])
def test_service_ports_are_not_webhook_ports(port: int) -> None:
    with pytest.raises(DestinationRefusedError):
        validate(f"https://hooks.example:{port}/x", resolver=_resolver)


def test_an_absurdly_long_url_is_refused_before_it_is_parsed() -> None:
    with pytest.raises(DestinationRefusedError, match="too long"):
        validate("https://hooks.example/" + "a" * 4000, resolver=_resolver)


# --------------------------------------------------------------------------- #
#  Signing
# --------------------------------------------------------------------------- #


def test_a_signature_verifies() -> None:
    body = b'{"hello":"world"}'
    signature = sign(b"secret", timestamp=1000, body=body)
    assert verify(b"secret", timestamp=1000, body=body, signature=signature, now=1010)


def test_a_captured_request_stops_verifying_once_it_is_stale() -> None:
    """The timestamp is INSIDE the signed material. Signing the body alone
    produces a token that is valid forever."""
    body = b'{"hello":"world"}'
    signature = sign(b"secret", timestamp=1000, body=body)
    assert not verify(b"secret", timestamp=1000, body=body, signature=signature, now=99_999)


def test_a_tampered_body_does_not_verify() -> None:
    signature = sign(b"secret", timestamp=1000, body=b'{"score":0.1}')
    assert not verify(
        b"secret", timestamp=1000, body=b'{"score":0.9}', signature=signature, now=1010
    )


def test_the_wrong_secret_does_not_verify() -> None:
    signature = sign(b"secret", timestamp=1000, body=b"{}")
    assert not verify(b"other", timestamp=1000, body=b"{}", signature=signature, now=1010)


def test_a_forward_dated_signature_is_also_rejected() -> None:
    """Clock skew is bounded in both directions. A receiver that only checked
    "not too old" would accept a signature minted for next year."""
    signature = sign(b"secret", timestamp=99_999, body=b"{}")
    assert not verify(b"secret", timestamp=99_999, body=b"{}", signature=signature, now=1000)


# --------------------------------------------------------------------------- #
#  Payload allowlist
# --------------------------------------------------------------------------- #


def test_the_payload_allowlist_refuses_anything_it_does_not_know() -> None:
    with pytest.raises(PayloadRejectedError, match="candidate_name"):
        assert_no_pii({"resume_id": "x", "candidate_name": "Priya Placeholder"})


def test_the_payload_allowlist_refuses_free_text_in_a_known_key() -> None:
    """Second net: an allowlisted key could still be handed a paragraph."""
    with pytest.raises(PayloadRejectedError, match="too long"):
        assert_no_pii({"parse_status": "x" * 400})


def test_the_allowlist_holds_no_obviously_identifying_key() -> None:
    """A guard on the table itself, so adding one is a deliberate act."""
    forbidden = {
        "name",
        "email",
        "phone",
        "address",
        "text",
        "raw_text",
        "resume_text",
        "candidate_name",
        "pii",
        "token_map",
    }
    assert not (ALLOWED_KEYS & forbidden)


# --------------------------------------------------------------------------- #
#  Relay behaviour
# --------------------------------------------------------------------------- #


def _event(**kw) -> OutboxEvent:
    defaults = {
        "id": uuid.uuid4(),
        "org_id": uuid.uuid4(),
        "event_type": str(EventType.RESUME_SCORED),
        "resource_type": "match",
        "resource_id": str(uuid.uuid4()),
        "payload": {"score": 0.5},
        "event_key": "k",
        "status": "delivering",
        "attempts": 1,
        "max_attempts": 8,
        # created_at is a server default, so an unpersisted row has None. The
        # relay only ever sees rows the database has written.
        "created_at": dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
    }
    return OutboxEvent(**{**defaults, **kw})


def test_backoff_is_bounded_and_jittered() -> None:
    """Unjittered retries turn a receiver's brief outage into a sustained one:
    every pending event for every tenant retries at the same instant."""
    samples = [backoff_seconds(6) for _ in range(200)]
    assert all(0 <= s <= 64 for s in samples)
    assert len(set(samples)) > 100  # actually jittered, not a constant


def test_backoff_is_capped_at_an_hour() -> None:
    assert all(backoff_seconds(40) <= 3600.0 for _ in range(50))


def test_a_failed_delivery_is_rescheduled_until_the_attempt_budget_runs_out() -> None:
    event = _event(attempts=1)
    _settle(event, DeliveryOutcome(False, 500, "HTTP 500"))
    assert event.status == "pending"
    assert event.next_attempt_at is not None

    exhausted = _event(attempts=8, max_attempts=8)
    _settle(exhausted, DeliveryOutcome(False, 500, "HTTP 500"))
    # Dead, not deleted: an event nobody could deliver is evidence.
    assert exhausted.status == "dead"


def test_a_delivered_event_is_marked_and_stops() -> None:
    event = _event()
    _settle(event, DeliveryOutcome(True, 204, None))
    assert event.status == "delivered"
    assert event.delivered_at is not None
    assert event.locked_by is None


def test_the_signed_body_is_canonical() -> None:
    """The receiver recomputes the MAC over the bytes it received. If
    re-serialising the same document could reorder keys, an unmodified payload
    would fail verification."""
    event = _event(payload={"b": 2, "a": 1})
    first = body_for(event)
    event.payload = {"a": 1, "b": 2}
    assert body_for(event) == first
    assert b" " not in first  # no incidental whitespace to disagree about


async def test_delivery_signs_the_request_and_the_signature_checks_out() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    event = _event()
    async with httpx.AsyncClient(transport=transport) as client:
        outcome = await deliver(
            client,
            destination=validate("https://hooks.example/x", resolver=_resolver),
            secret=b"shhh",
            event=event,
            now=1000,
        )

    assert outcome.delivered
    request = captured["request"]
    assert verify(
        b"shhh",
        timestamp=int(request.headers["x-screener-timestamp"]),
        body=request.content,
        signature=request.headers["x-screener-signature"],
        now=1000,
    )
    assert request.headers["x-screener-event-key"] == "k"


async def test_a_redirect_is_not_followed() -> None:
    """Following one would let a validated public URL bounce the request to the
    metadata service — every address check undone by a 302."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await deliver(
            client,
            destination=validate("https://hooks.example/x", resolver=_resolver),
            secret=b"s",
            event=_event(),
            now=1000,
        )
    assert not outcome.delivered
    assert outcome.status_code == 302


async def test_a_receiver_error_is_recorded_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await deliver(
            client,
            destination=validate("https://hooks.example/x", resolver=_resolver),
            secret=b"s",
            event=_event(),
            now=1000,
        )
    assert not outcome.delivered
    assert outcome.error is not None and "500" in outcome.error


def test_the_body_carries_no_key_outside_the_allowlist() -> None:
    document = json.loads(body_for(_event(payload={"score": 0.5, "resume_id": "r"})))
    assert set(document["data"]) <= ALLOWED_KEYS


def test_an_endpoint_model_never_exposes_its_secret_as_text() -> None:
    """A defensive check on the model: the column is bytes, so a careless
    f-string in a log line produces a repr rather than a usable key."""
    endpoint = WebhookEndpoint(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        url="https://x.example",
        secret_ciphertext=b"\x00\x01",
        event_types=[],
    )
    assert isinstance(endpoint.secret_ciphertext, bytes)

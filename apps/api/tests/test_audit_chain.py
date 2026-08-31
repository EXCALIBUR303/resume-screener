"""The audit chain must detect any edit. Pure-unit, no database needed."""

from __future__ import annotations

import uuid

import pytest

from screener_api.security.audit import (
    GENESIS,
    ChainBrokenError,
    canonical_json,
    compute_hash,
    hash_ip,
)


def test_canonical_json_is_order_independent() -> None:
    """Two structurally identical payloads must hash identically, whatever order
    the keys were built in — otherwise the chain is not reproducible."""
    a = canonical_json({"b": 1, "a": {"y": 2, "x": 1}})
    b = canonical_json({"a": {"x": 1, "y": 2}, "b": 1})
    assert a == b


def test_canonical_json_rejects_nan() -> None:
    with pytest.raises(ValueError, match="Out of range"):
        canonical_json({"x": float("nan")})


def test_hash_changes_when_any_field_changes() -> None:
    payload = {"action": "auth.login", "outcome": "success", "resource_id": "u1"}
    baseline = compute_hash(GENESIS, payload)
    for field, value in [
        ("action", "auth.logout"),
        ("outcome", "failure"),
        ("resource_id", "u2"),
    ]:
        assert compute_hash(GENESIS, {**payload, field: value}) != baseline


def test_hash_changes_when_predecessor_changes() -> None:
    """This is what makes it a chain: altering row N invalidates N+1 onward."""
    payload = {"action": "a"}
    assert compute_hash("a" * 64, payload) != compute_hash("b" * 64, payload)


def test_ip_is_hashed_not_stored() -> None:
    hashed = hash_ip("203.0.113.7")
    assert hashed is not None
    assert "203.0.113.7" not in hashed
    assert len(hashed) == 64
    assert hash_ip(None) is None


def _link(events: list[dict[str, object]]) -> list[tuple[str, str, dict[str, object]]]:
    """Build a valid chain: (prev_hash, hash, payload) triples."""
    chain, prev = [], GENESIS
    for payload in events:
        h = compute_hash(prev, payload)
        chain.append((prev, h, payload))
        prev = h
    return chain


def _verify(chain: list[tuple[str, str, dict[str, object]]]) -> int:
    expected_prev = GENESIS
    for seq, (prev, stored_hash, payload) in enumerate(chain, start=1):
        if prev != expected_prev:
            raise ChainBrokenError(seq, "prev_hash mismatch")
        if compute_hash(prev, payload) != stored_hash:
            raise ChainBrokenError(seq, "contents do not match hash")
        expected_prev = stored_hash
    return len(chain)


def test_intact_chain_verifies() -> None:
    chain = _link([{"action": f"event.{i}"} for i in range(100)])
    assert _verify(chain) == 100


def test_edited_row_is_detected() -> None:
    chain = _link([{"action": f"event.{i}"} for i in range(20)])
    prev, stored_hash, payload = chain[9]
    chain[9] = (prev, stored_hash, {**payload, "action": "event.tampered"})
    with pytest.raises(ChainBrokenError) as exc:
        _verify(chain)
    assert exc.value.seq == 10


def test_deleted_row_is_detected() -> None:
    """Removing a row breaks the link between its neighbours."""
    chain = _link([{"action": f"event.{i}"} for i in range(20)])
    del chain[9]
    with pytest.raises(ChainBrokenError) as exc:
        _verify(chain)
    assert exc.value.seq == 10


def test_rehashing_the_edited_row_still_breaks_the_next_one() -> None:
    """The realistic attack: edit a row AND recompute its hash. The chain still
    breaks, because the following row's prev_hash no longer matches."""
    chain = _link([{"action": f"event.{i}"} for i in range(20)])
    prev, _, payload = chain[9]
    forged = {**payload, "action": "event.tampered"}
    chain[9] = (prev, compute_hash(prev, forged), forged)
    with pytest.raises(ChainBrokenError) as exc:
        _verify(chain)
    assert exc.value.seq == 11  # the row after the forgery
    assert "prev_hash" in exc.value.reason


def test_full_rewrite_from_the_edit_onward_is_the_only_way_through() -> None:
    """An attacker must rewrite every subsequent row to hide one edit. That is
    the guarantee — not prevention, but a cost that cannot be paid quietly."""
    original = [
        {"action": f"event.{i}", "id": str(uuid.uuid5(uuid.NAMESPACE_OID, str(i)))}
        for i in range(20)
    ]
    forged_source = [*original]
    forged_source[9] = {**forged_source[9], "action": "event.tampered"}
    assert _verify(_link(forged_source)) == 20  # internally consistent...
    # ...but every hash from seq 10 on differs from the genuine chain, so any
    # external record of a later hash (a log line, a backup) exposes it.
    genuine, forged = _link(original), _link(forged_source)
    assert genuine[9][1] != forged[9][1]
    assert [h for _, h, _ in genuine[10:]] != [h for _, h, _ in forged[10:]]

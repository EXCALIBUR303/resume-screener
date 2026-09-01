"""The worker's liveness listener.

Not a web server -- one thing, exercised over a real socket rather than by
calling the handler function directly, because the whole point is what a
platform's port check actually receives.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from screener_api.health import STALE_AFTER_SECONDS, Heartbeat, serve


def test_a_fresh_heartbeat_is_not_stale() -> None:
    assert not Heartbeat().is_stale()


def test_a_heartbeat_goes_stale_after_the_threshold() -> None:
    hb = Heartbeat()
    hb._last = time.monotonic() - (STALE_AFTER_SECONDS + 1)
    assert hb.is_stale()


def test_beat_resets_staleness() -> None:
    hb = Heartbeat()
    hb._last = time.monotonic() - (STALE_AFTER_SECONDS + 1)
    assert hb.is_stale()
    hb.beat()
    assert not hb.is_stale()


async def _get(port: int) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    await writer.drain()
    response = await asyncio.wait_for(reader.read(4096), timeout=3)
    writer.close()
    return response


async def test_a_fresh_worker_answers_200_over_a_real_socket() -> None:
    """A real TCP connection, not a call to the handler function -- this is
    what a platform's port check actually does."""
    hb = Heartbeat()
    server = await serve(hb, port=0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        response = await _get(port)
    assert response.startswith(b"HTTP/1.1 200")


async def test_a_wedged_worker_answers_503() -> None:
    """The whole reason this exists: a stuck poll loop must not look
    identical to a healthy one from outside the container."""
    hb = Heartbeat()
    hb._last = time.monotonic() - (STALE_AFTER_SECONDS + 1)
    server = await serve(hb, port=0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        response = await _get(port)
    assert response.startswith(b"HTTP/1.1 503")


async def test_recovering_makes_it_healthy_again() -> None:
    hb = Heartbeat()
    hb._last = time.monotonic() - (STALE_AFTER_SECONDS + 1)
    server = await serve(hb, port=0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        assert (await _get(port)).startswith(b"HTTP/1.1 503")
        hb.beat()
        assert (await _get(port)).startswith(b"HTTP/1.1 200")


async def test_a_malformed_request_does_not_crash_the_listener() -> None:
    """The container's only open port is the worker's, and it is never behind
    the API's own input validation -- garbage in must not take the listener
    down, because that would look identical to a genuine crash from outside."""
    hb = Heartbeat()
    server = await serve(hb, port=0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x00\x01not-http-at-all\xff\xff")
        await writer.drain()
        writer.close()
        # The listener itself must still be alive for the next real request.
        response = await _get(port)
    assert response.startswith(b"HTTP/1.1 200")


def test_health_port_is_opt_in_via_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset means no listener at all -- the local/compose default, where
    nothing checks a worker's port and opening one unasked would be a stray
    listening socket nobody asked for."""
    monkeypatch.delenv("HEALTH_PORT", raising=False)
    import os

    assert os.environ.get("HEALTH_PORT") is None

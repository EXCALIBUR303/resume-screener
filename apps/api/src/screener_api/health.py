"""A liveness listener for processes that have no other reason to bind a port.

The worker is a pure background consumer — nothing calls it over HTTP. Most
hosts that run a container assume otherwise: Hugging Face Spaces and similar
platforms check that *something* answers on a port before calling a deployment
healthy, and a process that never listens looks identical to one that crashed
on boot.

This is not a web server. It answers exactly one thing — "is the poll loop
still ticking" — by comparing the current time to a heartbeat the caller
updates every iteration. A worker wedged inside a stuck job (network hang,
runaway parse) stops updating the heartbeat and this starts returning 503,
which is what makes it a genuine liveness check rather than "the process
exists."
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import structlog

log = structlog.get_logger()

# Generous relative to a single poll interval, but tight enough to catch a
# genuinely wedged loop rather than only a crashed process. The slowest normal
# step is an LLM call, capped at LLM_TIMEOUT_SECONDS (60s default) plus one
# retry — comfortably under this.
STALE_AFTER_SECONDS = 180.0


class Heartbeat:
    """Shared between the poll loop and the health listener.

    A plain float behind a class rather than a bare module global: the worker
    module already has one process-wide mutable flag (`_stopping`) and a
    second one with the same shape invites them to be confused for each other.
    """

    def __init__(self) -> None:
        self._last = time.monotonic()

    def beat(self) -> None:
        self._last = time.monotonic()

    def is_stale(self) -> bool:
        return (time.monotonic() - self._last) > STALE_AFTER_SECONDS


async def serve(heartbeat: Heartbeat, *, port: int) -> asyncio.base_events.Server:
    """Start the listener. Runs alongside the poll loop, never blocks it."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Don't parse the request — the response is identical regardless of
        # path or method. Just drain enough to let the client's HTTP stack
        # consider the request sent before we reply.
        with contextlib.suppress(TimeoutError, ConnectionError, OSError):
            await asyncio.wait_for(reader.read(1024), timeout=5)
        healthy = not heartbeat.is_stale()
        status, body = (b"200 OK", b"ok") if healthy else (b"503 Service Unavailable", b"stale")
        writer.write(
            b"HTTP/1.1 " + status + b"\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        with contextlib.suppress(ConnectionError, OSError):
            await writer.drain()
        writer.close()

    # 0.0.0.0 deliberately: this runs inside a container, and the container's
    # own network boundary is the actual perimeter — binding to localhost only
    # would make the port unreachable from outside it, which defeats the point.
    server = await asyncio.start_server(handle, "0.0.0.0", port)  # nosec B104
    log.info("health.listening", port=port)
    return server

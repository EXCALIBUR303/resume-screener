"""Request correlation and access logging."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

log = structlog.get_logger()

# Header is accepted from upstream so a proxy can correlate, but it is validated:
# an attacker-supplied value must not be able to poison log parsing.
HEADER = "x-request-id"


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(HEADER, "")
        request_id = incoming if _is_safe_id(incoming) else str(uuid.uuid4())

        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request.failed", duration_ms=_ms(started))
            structlog.contextvars.clear_contextvars()
            raise

        response.headers[HEADER] = request_id
        log.info("request.completed", status=response.status_code, duration_ms=_ms(started))
        structlog.contextvars.clear_contextvars()
        return response


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _is_safe_id(value: str) -> bool:
    return 0 < len(value) <= 64 and all(c.isalnum() or c in "-_" for c in value)

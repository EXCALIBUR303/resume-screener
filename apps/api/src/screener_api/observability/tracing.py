"""Tracing, including across the queue boundary.

The interesting span is upload → parse → embed → score, and those happen in
three processes connected by a database table. Context is therefore carried in
the job payload: without that, a "trace" would be four unrelated single-span
traces and would answer none of the questions tracing exists for.
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger()

_enabled = False


def configure_tracing(*, enabled: bool, endpoint: str, service: str) -> None:
    """Optional by design: the default stack runs without a collector, and an
    observability dependency that breaks the app when absent is a liability."""
    global _enabled
    if not enabled:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": service}))
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
            )
        except ImportError:
            log.warning("tracing.exporter_missing", hint="pip install opentelemetry-exporter-otlp")
        trace.set_tracer_provider(provider)
        _enabled = True
        log.info("tracing.enabled", endpoint=endpoint, service=service)
    except Exception as exc:
        log.warning("tracing.unavailable", error=type(exc).__name__)


def tracer() -> Any:
    from opentelemetry import trace

    return trace.get_tracer("screener")


def inject_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach the current trace context to a job payload.

    W3C traceparent, so the span the worker opens is a child of the request that
    enqueued it and the whole pipeline reads as one trace.
    """
    if not _enabled:
        return payload
    try:
        from opentelemetry.propagate import inject

        carrier: dict[str, str] = {}
        inject(carrier)
        return {**payload, "_trace": carrier}
    except Exception:
        return payload


def extract_context(payload: dict[str, Any]) -> Any:
    if not _enabled:
        return None
    try:
        from opentelemetry.propagate import extract

        carrier = payload.get("_trace")
        return extract(carrier) if isinstance(carrier, dict) else None
    except Exception:
        return None

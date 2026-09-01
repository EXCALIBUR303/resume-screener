"""Worker entry point.

Two pools with different privileges, selected by ``WORKER_TYPES``:

* ``parse``  — runs with ``network_mode: none``. It touches attacker-controlled
  bytes, so it is given nothing to exfiltrate with.
* ``embed,score`` — reaches the model host only.

The split is enforced by compose, not by convention.
"""

from __future__ import annotations

import asyncio
import os
import resource
import signal
import socket
import sys
from typing import Any

import structlog

from screener_api.db import dispose_engine, init_engine
from screener_api.ingest.storage import BlobStore
from screener_api.llm.factory import build_gateway
from screener_api.llm.prompts import active_version, load
from screener_api.logging import configure_logging
from screener_api.parse.pipeline import handle_parse_job
from screener_api.queue import (
    ErrorClass,
    JobType,
    TerminalError,
    claim,
    complete,
    fail,
    reclaim_expired_leases,
)
from screener_api.retrieval.pipeline import handle_embed_job
from screener_api.scoring.pipeline import handle_score_job
from screener_api.security.crypto import derive_kek
from screener_api.settings import get_settings

log = structlog.get_logger()
_stopping = False


def _apply_rlimits(max_memory_mb: int) -> None:
    """Hard ceiling on address space for the PARSE pool only.

    RLIMIT_AS counts *virtual* address space, and onnxruntime reserves far more
    of it than it ever resides — a 1 GB cap made the embedding model fail with
    std::bad_alloc. The limit exists to stop a decompression bomb in the worker
    that handles hostile bytes; the AI pool parses nothing untrusted and is
    bounded by the container's mem_limit instead.
    """
    limit = max_memory_mb * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError) as exc:  # pragma: no cover - platform dependent
        log.warning("worker.rlimit_unavailable", error=str(exc))


def _handle_signal(signum: int, _frame: Any) -> None:
    global _stopping
    _stopping = True
    log.info("worker.stopping", signal=signum)


async def run() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    types = [JobType(t) for t in os.environ.get("WORKER_TYPES", "parse").split(",") if t]
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    # Only the pool that touches attacker-controlled bytes gets the hard cap.
    if types == [JobType.PARSE]:
        _apply_rlimits(settings.parse_memory_limit_mb)

    init_engine(settings)
    from screener_api.db import _sessionmaker

    if _sessionmaker is None:
        raise RuntimeError("engine not initialised")

    kek = derive_kek(settings.app_kek.get_secret_value(), settings.app_kek_version)
    store = BlobStore(settings.storage_local_path, kek=kek, kek_version=settings.app_kek_version)

    # Built once per process: the gateway carries the token budget and circuit
    # breaker, which must be shared across jobs to mean anything.
    gateway = build_gateway(settings)
    prompt = load("match_score", active_version("match_score", settings.llm_prompt_version))
    log.info(
        "worker.started",
        worker=worker_id,
        types=[str(t) for t in types],
        prompt=prompt.version_id,
        prompt_hash=prompt.content_hash[:12],
    )
    idle_ticks = 0

    while not _stopping:
        failure: tuple[Any, BaseException, ErrorClass] | None = None
        async with _sessionmaker() as session:
            # Any worker may reclaim: the sweeper does not need its own process.
            if idle_ticks % 60 == 0:
                reclaimed = await reclaim_expired_leases(
                    session, timeout_seconds=settings.worker_lease_timeout_seconds
                )
                if reclaimed:
                    log.warning("worker.leases_reclaimed", count=reclaimed)
                await session.commit()

            job = await claim(session, worker=worker_id, job_types=types)
            if job is None:
                await session.commit()
                idle_ticks += 1
                await asyncio.sleep(settings.worker_poll_interval_ms / 1000)
                continue

            idle_ticks = 0
            # Read identifiers BEFORE the try: after session.rollback() the ORM
            # object is expired, and touching job.id triggers a lazy refresh —
            # IO outside the greenlet, which raised MissingGreenlet and left the
            # job stuck in 'running' until the lease sweeper rescued it.
            job_id = job.id
            job_type = job.job_type
            structlog.contextvars.bind_contextvars(job_id=str(job_id), job_type=job_type)
            try:
                if job_type == str(JobType.PARSE):
                    await handle_parse_job(
                        session,
                        dict(job.payload),
                        store=store,
                        kek=kek,
                        kek_version=settings.app_kek_version,
                    )
                elif job_type == str(JobType.EMBED):
                    await handle_embed_job(session, dict(job.payload))
                elif job_type == str(JobType.SCORE):
                    await handle_score_job(
                        session, dict(job.payload), gateway=gateway, prompt=prompt
                    )
                else:
                    raise TerminalError(f"no handler for job type {job_type}")
                await complete(session, job)
                await session.commit()
            except TerminalError as exc:
                await session.rollback()
                failure = (job_id, exc, ErrorClass.TERMINAL)
            except Exception as exc:
                await session.rollback()
                log.exception("job.failed")
                failure = (job_id, exc, ErrorClass.RETRYABLE)
            finally:
                structlog.contextvars.clear_contextvars()

        # Bookkeeping runs OUTSIDE the failed session's context. Opening a
        # second session while the first was unwinding raised MissingGreenlet,
        # which left the job stuck in 'running' until the lease sweeper
        # rescued it minutes later.
        if failure is not None:
            await _record_failure(*failure)

    await dispose_engine()
    log.info("worker.stopped")
    return 0


async def _record_failure(job_id: Any, error: BaseException, error_class: ErrorClass) -> None:
    """Failure bookkeeping runs in its own transaction: the job's own
    transaction was rolled back, and losing the error record would strand the
    job as permanently running."""
    from sqlalchemy import select

    from screener_api.db import _sessionmaker
    from screener_api.models import JobQueue

    if _sessionmaker is None:  # pragma: no cover
        return
    async with _sessionmaker() as bookkeeping:
        job = (
            await bookkeeping.execute(select(JobQueue).where(JobQueue.id == job_id))
        ).scalar_one_or_none()
        if job is not None:
            await fail(bookkeeping, job, error=error, error_class=error_class)
            await bookkeeping.commit()


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())

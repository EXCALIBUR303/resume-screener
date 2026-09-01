"""Queue gauges, sampled at scrape time.

Sampling on scrape rather than tracking increments in the workers: the queue is
already the single source of truth, and a counter maintained in two processes
would drift from it. One cheap aggregate query is more honest than bookkeeping.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from screener_api.observability.metrics import (
    dead_letter_depth,
    oldest_pending_seconds,
    queue_depth,
)


async def collect_queue_depths(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            text("SELECT job_type, status, count(*) FROM job_queue GROUP BY 1, 2")
        )
    ).all()

    # Clear first: a series that stops being reported would otherwise keep its
    # last value forever, so a drained queue would read as permanently backed up.
    queue_depth.clear()
    dead = 0
    for job_type, status, count in rows:
        queue_depth.labels(job_type=job_type, status=status).set(count)
        if status == "dead":
            dead += count
    dead_letter_depth.set(dead)

    oldest = (
        await session.execute(
            text(
                "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - MIN(run_after))), 0) "
                "FROM job_queue WHERE status = 'pending'"
            )
        )
    ).scalar_one()
    oldest_pending_seconds.set(float(oldest or 0))

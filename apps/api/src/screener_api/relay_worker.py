"""Webhook relay process.

A separate service from `worker-ai`, and the reason is privilege rather than
tidiness. `worker-ai` reaches one destination we control — the model host.
This process reaches URLs a *tenant* typed in, on the public internet. Putting
arbitrary outbound egress in the process that also runs the scoring pipeline
would give an attacker who reached that pipeline a way out; keeping them apart
means the blast radii do not overlap.

It derives its key with `purpose="webhook"`, so the key this process holds
decrypts endpoint signing secrets and nothing else. It never reads a resume, a
PII map or a blob.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import time
from typing import Any

import httpx
import structlog

from screener_api.db import dispose_engine, init_engine
from screener_api.logging import configure_logging
from screener_api.outbox.relay import claim, process
from screener_api.security.crypto import WEBHOOK_KEY_PURPOSE, derive_kek
from screener_api.settings import get_settings

log = structlog.get_logger()

_stopping = False
POLL_INTERVAL_SECONDS = 2.0
BATCH = 10


def _handle_signal(signum: int, _frame: Any) -> None:
    global _stopping
    _stopping = True
    log.info("relay.stopping", signal=signum)


async def run() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    init_engine(settings)
    from screener_api.db import _sessionmaker

    if _sessionmaker is None:
        raise RuntimeError("engine not initialised")

    kek = derive_kek(
        settings.app_kek.get_secret_value(),
        settings.app_kek_version,
        purpose=WEBHOOK_KEY_PURPOSE,
    )
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    log.info("relay.started", worker=worker_id)

    # One client for the process: connection reuse matters when the same
    # receiver gets many events. Redirects stay off at the client level too, so
    # a future call site cannot re-enable them by forgetting the argument.
    async with httpx.AsyncClient(follow_redirects=False) as client:
        while not _stopping:
            delivered = 0
            async with _sessionmaker() as session:
                events = await claim(session, worker=worker_id, limit=BATCH)
                if not events:
                    await session.commit()
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    continue

                # Wall clock, not the loop clock: the timestamp is signed and
                # the receiver compares it against its own clock to reject
                # replays. A monotonic counter since process start would fail
                # every tolerance check.
                now = int(time.time())
                for event in events:
                    outcome = await process(
                        session,
                        client,
                        event,
                        kek=kek,
                        now=now,
                        allow_private=settings.webhook_allow_private_destinations,
                    )
                    delivered += int(outcome.delivered)
                await session.commit()

            log.info("relay.batch", claimed=len(events), delivered=delivered)

    await dispose_engine()
    log.info("relay.stopped")
    return 0


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())

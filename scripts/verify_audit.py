"""Walk the audit hash chain from the command line. Exit 1 on any break."""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from screener_api.security.audit import ChainBrokenError, verify_chain  # noqa: E402
from screener_api.settings import get_settings  # noqa: E402


async def main() -> int:
    engine = create_async_engine(get_settings().dsn)
    try:
        async with async_sessionmaker(engine)() as session:
            try:
                count = await verify_chain(session)
            except ChainBrokenError as exc:
                print(f"AUDIT CHAIN BROKEN at seq={exc.seq}: {exc.reason}")
                return 1
            print(f"audit chain intact: {count} events verified")
            return 0
    finally:
        await engine.dispose()


raise SystemExit(asyncio.run(main()))

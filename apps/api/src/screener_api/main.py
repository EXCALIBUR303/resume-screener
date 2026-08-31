"""Application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI
from sqlalchemy import text

from screener_api.db import dispose_engine, init_engine
from screener_api.logging import configure_logging
from screener_api.middleware import RequestContextMiddleware
from screener_api.routers import admin, auth, resumes
from screener_api.settings import Settings, get_settings

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)
    init_engine(settings)
    if placeholders := settings.placeholder_secrets():
        # Dev only — outside dev the settings validator has already refused to start.
        log.warning("settings.placeholder_secrets_in_use", fields=placeholders)
    log.info("app.started", env=settings.app_env, name=settings.app_name)
    yield
    await dispose_engine()
    log.info("app.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Secure AI Resume Screener",
        version="0.1.0",
        # Docs are useful in dev and an information leak in prod.
        docs_url="/docs" if settings.app_env != "prod" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.app_env != "prod" else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(resumes.router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        """Liveness: is the process up? Never touches the database."""
        return {"status": "ok", "env": settings.app_env}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> dict[str, Any]:
        """Readiness: can we actually serve? Checks the database and pgvector."""
        from screener_api.db import _sessionmaker

        if _sessionmaker is None:
            return {"status": "starting", "database": "not-initialised"}
        try:
            async with _sessionmaker() as session:
                version = (
                    await session.execute(
                        text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                    )
                ).scalar_one_or_none()
        except Exception as exc:
            log.warning("readyz.database_unreachable", error=type(exc).__name__)
            return {"status": "degraded", "database": "unreachable"}
        return {
            "status": "ok" if version else "degraded",
            "database": "ok",
            "pgvector": version or "missing",
        }

    return app


app = create_app()

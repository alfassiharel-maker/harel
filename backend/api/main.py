"""FastAPI application factory.

`create_app` builds the whole object graph and returns an app. It takes an
optional `Settings` so a test can pass a `ci`-environment configuration without
mutating the process environment or the settings cache.

The long-lived services are constructed in `lifespan`, once, and disposed on
shutdown. The database engine in particular owns a connection pool that must be
closed cleanly — leaking it across a test suite exhausts Postgres connections.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from backend.api.context import request_context_middleware
from backend.api.deps import AppServices
from backend.api.errors import install_exception_handlers
from backend.api.routers import auth, health, me, training
from backend.core.config import Settings, get_settings
from backend.core.logging import configure_logging, get_logger
from backend.core.security import PasswordService, TokenService
from backend.database.session import Database
from backend.modules.identity import IdentityService
from backend.modules.training import TrainingService

__all__ = ["create_app"]

logger = get_logger(__name__)


def _build_services(settings: Settings) -> AppServices:
    database = Database(settings)
    passwords = PasswordService()
    tokens = TokenService(settings)
    identity = IdentityService(database=database, settings=settings, passwords=passwords, tokens=tokens)
    training = TrainingService(database=database)
    return AppServices(
        settings=settings,
        database=database,
        passwords=passwords,
        tokens=tokens,
        identity=identity,
        training=training,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        services = _build_services(settings)
        app.state.services = services
        logger.info("api_startup", env=settings.app_env)
        try:
            yield
        finally:
            await services.database.dispose()
            logger.info("api_shutdown")

    app = FastAPI(
        title="AI Sports Coach API",
        version="0.1.0",
        # /docs and the OpenAPI schema are off in production: the schema is
        # published through the CI-generated clients, and an interactive explorer
        # on the production origin is attack surface, not a feature.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )

    app.middleware("http")(request_context_middleware)
    install_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(auth.router, prefix="/v1")
    app.include_router(me.router, prefix="/v1")
    app.include_router(training.router, prefix="/v1")

    return app

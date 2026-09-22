"""Application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters.mongo import Mongo
from app.api.health import router as health_router
from app.config import Settings, get_settings
from app.logging import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    app.state.mongo = Mongo(settings)
    logger.info("apchi starting", extra={"environment": settings.environment})
    yield
    await app.state.mongo.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Apchi",
        version="0.1.0",
        summary="Control plane for configuring one Trino cluster",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(health_router, prefix="/api/v1")
    return app


app = create_app()

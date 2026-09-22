"""Application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters.mongo import Mongo
from app.api import errors
from app.api.applies import router as applies_router
from app.api.candidate import router as candidate_router
from app.api.catalogs import router as catalogs_router
from app.api.health import router as health_router
from app.config import Settings, get_settings
from app.logging import configure_logging
from app.pipeline.applies import ApplyRunner, ApplyStore, NoOpEngine, recover_interrupted
from app.pipeline.candidate import CandidateStore

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    app.state.mongo = Mongo(settings)
    app.state.candidate_store = CandidateStore(app.state.mongo.database)
    app.state.apply_store = ApplyStore(app.state.mongo.database)
    app.state.apply_runner = ApplyRunner(app.state.apply_store, NoOpEngine())
    logger.info("apchi starting", extra={"environment": settings.environment})

    # An Apply left in flight by a restart would freeze the Candidate forever.
    #
    # If MongoDB is unreachable this cannot run, and the choice is between refusing
    # to start and starting degraded. Starting degraded wins: the health endpoint
    # already reports 503 while MongoDB is down, which is the readiness signal
    # Kubernetes acts on, and a pod that crashloops on a transient blip loses its
    # logs and is harder to diagnose than one reporting unhealthy. Recovery is
    # retried at the next start.
    try:
        recovered = await recover_interrupted(app.state.apply_store)
    except Exception:
        logger.exception(
            "startup recovery did not run; a Candidate frozen by an interrupted "
            "Apply will stay frozen until a start that can reach MongoDB"
        )
    else:
        if recovered:
            logger.warning("released the Candidate", extra={"recovered": len(recovered)})

    yield
    await app.state.apply_runner.shutdown()
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
    errors.install(app)
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(catalogs_router, prefix="/api/v1")
    app.include_router(candidate_router, prefix="/api/v1")
    app.include_router(applies_router, prefix="/api/v1")
    return app


app = create_app()

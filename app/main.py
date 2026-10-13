"""Application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.adapters.kubernetes import RealKubernetes
from app.adapters.mongo import Mongo
from app.adapters.trino import Trino
from app.api import errors
from app.api.admin import router as admin_router
from app.api.adoption import router as adoption_router
from app.api.applies import router as applies_router
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.candidate import router as candidate_router
from app.api.catalogs import router as catalogs_router
from app.api.certificate_mapping import router as certificate_mapping_router
from app.api.client_certificates import router as certificates_router
from app.api.deps import AdminOnly, Authenticated
from app.api.event_listeners import router as event_listeners_router
from app.api.health import router as health_router
from app.api.permissions import router as permissions_router
from app.api.resource_groups import router as resource_groups_router
from app.api.snapshots import router as snapshots_router
from app.api.tokens import router as tokens_router
from app.api.validations import router as validations_router
from app.audit.events import AuditStore
from app.auth.logins import LoginStore
from app.auth.oidc import OidcProvider
from app.auth.sessions import SessionStore
from app.auth.tokens import TokenStore
from app.auth.users import UserStore, bootstrap
from app.config import AuthMode, Settings, get_settings
from app.logging import configure_logging
from app.pipeline.admin_values import AdminStore
from app.pipeline.applies import ApplyEngine, ApplyRunner, ApplyStore, recover_interrupted
from app.pipeline.candidate import CandidateStore
from app.pipeline.discoveries import DiscoveryStore
from app.pipeline.engine import AdminEngine, Engine
from app.pipeline.high_water import HighWaterMark
from app.pipeline.maintenance import MaintenanceStore
from app.pipeline.snapshots import SnapshotStore
from app.pipeline.validation import VALIDATION_SELECTOR
from app.pipeline.validations import ValidationRunner, ValidationStore
from app.version import VERSION

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    app.state.mongo = Mongo(settings)
    app.state.snapshot_store = SnapshotStore(app.state.mongo.database)
    # Indexed on its first write rather than here. An index on an empty collection has no
    # reader, and three create_index round trips per process start is what wedged the test
    # MongoDB in #131 -- tier 1 starts an app per test. See AuditStore.
    app.state.audit_store = AuditStore(
        app.state.mongo.database, retention_days=settings.audit_retention_days
    )
    # The Candidate store writes §20's audit at its one chokepoint, so it needs the audit
    # store rather than each route carrying one.
    app.state.candidate_store = CandidateStore(
        app.state.mongo.database, app.state.snapshot_store, app.state.audit_store
    )
    app.state.apply_store = ApplyStore(app.state.mongo.database)
    app.state.validation_store = ValidationStore(app.state.mongo.database)
    app.state.maintenance_store = MaintenanceStore(app.state.mongo.database, app.state.audit_store)
    app.state.admin_store = AdminStore(app.state.mongo.database, app.state.audit_store)
    app.state.discovery_store = DiscoveryStore(app.state.mongo.database)
    app.state.user_store = UserStore(app.state.mongo.database)
    app.state.session_store = SessionStore(app.state.mongo.database)
    app.state.token_store = TokenStore(app.state.mongo.database, app.state.audit_store)
    app.state.login_store = LoginStore(app.state.mongo.database)
    # One per app, so discovery and the provider's JWKS are fetched once rather than per
    # login. Tests substitute it to serve a provider in process.
    if not hasattr(app.state, "oidc"):
        app.state.oidc = OidcProvider(settings)

    # Tests substitute the Kubernetes adapter before the lifespan runs; nothing else
    # is ever substituted.
    if not hasattr(app.state, "kubernetes"):
        app.state.kubernetes = RealKubernetes(namespace=settings.kubernetes_namespace)
    if not hasattr(app.state, "trino"):
        app.state.trino = Trino(
            host=settings.trino_host, port=settings.trino_port, user=settings.trino_user
        )

    def build(engine: type[Engine], apply_id: str) -> ApplyEngine:
        return engine(
            apply_id=apply_id,
            settings=settings,
            candidates=app.state.candidate_store,
            snapshots=app.state.snapshot_store,
            trino=app.state.trino,
            kubernetes=app.state.kubernetes,
            maintenance=app.state.maintenance_store,
            admin=app.state.admin_store,
        )

    def build_engine(apply_id: str) -> ApplyEngine:
        return build(Engine, apply_id)

    def build_admin_engine(apply_id: str) -> ApplyEngine:
        return build(AdminEngine, apply_id)

    app.state.apply_runner = ApplyRunner(app.state.apply_store, build_engine, app.state.audit_store)
    app.state.admin_apply_runner = ApplyRunner(
        app.state.apply_store, build_admin_engine, app.state.audit_store, category="admin"
    )
    app.state.validation_runner = ValidationRunner(app.state.validation_store, build_engine)
    app.state.high_water = HighWaterMark(app.state.kubernetes, settings)
    logger.info("apchi starting", extra={"environment": settings.environment})

    # An Apply left in flight by a restart would freeze the Candidate forever, and may
    # also have left the Cluster diverged -- so recovery unfreezes it and then rolls the
    # Cluster back, one attempt, exactly as a failed Apply does.
    #
    # If MongoDB is unreachable this cannot run, and the choice is between refusing
    # to start and starting degraded. Starting degraded wins: the health endpoint
    # already reports 503 while MongoDB is down, which is the readiness signal
    # Kubernetes acts on, and a pod that crashloops on a transient blip loses its
    # logs and is harder to diagnose than one reporting unhealthy. Recovery is
    # retried at the next start.
    try:
        recovered = await recover_interrupted(
            app.state.apply_store, build_engine, app.state.audit_store
        )
    except Exception:
        logger.exception(
            "startup recovery did not run; a Candidate frozen by an interrupted "
            "Apply will stay frozen until a start that can reach MongoDB"
        )
    else:
        if recovered:
            logger.warning("released the Candidate", extra={"recovered": len(recovered)})

    # Authentication storage, and only what the configured mode uses. In `none` mode nobody
    # signs in and nobody owns a token, so there are no sessions, no tokens and no users --
    # and indexing three collections that will stay empty is work with no reader.
    #
    # Not a micro-optimisation. Each of those is a round trip at startup, and tier 1 starts
    # an app per test: doing them unconditionally took the test MongoDB from about 4 to over
    # 600 connections created in 50 seconds and then wedged it outright, with current
    # connections never above 10 (#131). An index the mode cannot use is not worth that.
    #
    # Best-effort for the reason above: a MongoDB that is not there yet must not stop Apchi
    # starting, and all of it is idempotent, so the next start does it.
    if settings.auth_mode is not AuthMode.NONE:
        try:
            await app.state.session_store.ensure_indexes()
            await app.state.token_store.ensure_indexes()
            await app.state.user_store.ensure_indexes()
            await app.state.login_store.ensure_indexes()
            await bootstrap(
                app.state.user_store,
                settings.auth_initial_admin_username,
                settings.auth_initial_admin_password_hash,
            )
        except Exception:
            logger.exception("could not prepare authentication storage; retried at the next start")

    # A crash mid-validation leaks an ephemeral coordinator, and leaked Trino pods
    # are not cheap. The pods carry activeDeadlineSeconds as a second backstop, for
    # the Apchi that never comes back at all.
    try:
        orphans = await app.state.kubernetes.delete_pods(VALIDATION_SELECTOR)
        stale = await app.state.kubernetes.delete_secrets(VALIDATION_SELECTOR)
    except Exception:
        logger.exception("could not sweep orphaned validation objects")
    else:
        if orphans or stale:
            logger.warning(
                "deleted orphaned validation objects",
                extra={"pods": orphans, "secrets": stale},
            )

    yield
    await app.state.oidc.close()
    await app.state.apply_runner.shutdown()
    await app.state.admin_apply_runner.shutdown()
    await app.state.validation_runner.shutdown()
    await app.state.mongo.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Apchi",
        version=VERSION,
        summary="Control plane for configuring one Trino cluster",
        lifespan=lifespan,
    )
    app.state.settings = settings
    errors.install(app)
    # Documented on the contract rather than left to be discovered: these become reachable
    # the moment a mode other than `none` is configured, and the mode is runtime
    # configuration, so every gated operation can answer this way.
    unauthenticated: dict[int | str, dict[str, Any]] = {
        401: {"model": errors.ApiError, "description": "No usable credential."}
    }
    forbidden: dict[int | str, dict[str, Any]] = {
        403: {"model": errors.ApiError, "description": "Authenticated, but not an Admin."}
    }

    # Who may call what, in one place rather than per router, so the answer is readable
    # without opening thirteen files -- and asserted from the route table in tier 1, so a
    # router added later cannot be left ungated silently.
    #
    # /health carries no gate, deliberately. It is the chart's readiness *and* liveness
    # probe: a condition an Operator has to log in to clear would crashloop the pod they
    # need alive to reach it (see api/health.py).
    app.include_router(health_router, prefix="/api/v1")
    # Signing in cannot require being signed in. /auth/me carries no gate either: it answers
    # in every mode, so a UI asks it once rather than branching on the configured mode, and
    # in `none` mode it reports the anonymous Admin.
    app.include_router(auth_router, prefix="/api/v1", responses=unauthenticated)
    for router in (
        catalogs_router,
        certificate_mapping_router,
        certificates_router,
        permissions_router,
        resource_groups_router,
        event_listeners_router,
        candidate_router,
        applies_router,
        snapshots_router,
        validations_router,
        audit_router,
        tokens_router,
    ):
        app.include_router(
            router,
            prefix="/api/v1",
            dependencies=[Authenticated],
            responses=unauthenticated,
        )
    # Admin surfaces, both of them under /admin: these inject low-level configuration and
    # freeze mutation (§3), and PUT /admin/permissions/enforcement can open a Cluster to
    # every identity without a grant.
    for router in (admin_router, adoption_router):
        app.include_router(
            router,
            prefix="/api/v1",
            dependencies=[AdminOnly],
            responses=unauthenticated | forbidden,
        )
    return app


app = create_app()

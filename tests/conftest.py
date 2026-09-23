"""Test harness.

Two tiers, two seams. Every test drives the HTTP API; only the Kubernetes adapter
is ever substituted. MongoDB and Trino are real in both tiers.
"""

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from testcontainers.community.mongodb import MongoDbContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.adapters.kubernetes import PodState
from app.adapters.trino import Trino
from app.config import Environment, Settings
from app.main import create_app

# Pinned to match deploy/trino-dev and the supported range in section 8, so a
# Trino upgrade breaks CI rather than production.
TRINO_IMAGE = "trinodb/trino:483"

# Mirrors deploy/trino-dev: dynamic catalog management, with the store directory a
# plain path the non-root trino user can write. The stock TrinoContainer does not
# enable dynamic catalogs, so CREATE CATALOG would fail against it.
_DYNAMIC_CATALOGS = (
    'printf "\\ncatalog.management=dynamic\\ncatalog.store=file\\n"'
    " >> /etc/trino/config.properties"
    ' && printf "catalog.config-dir=/data/trino/catalogs\\n"'
    " > /etc/trino/catalog-store.properties"
    " && mkdir -p /data/trino/catalogs"
    " && exec /usr/lib/trino/bin/run-trino"
)


#: Mirrors the pod of app.pipeline.validation: dynamic catalogs through the image's
#: own environment variable, and the image's example catalogs -- jmx, memory, tpch,
#: tpcds -- removed, so the probe holds exactly what the Candidate declares. The
#: Cluster does not need this because pointing catalog.config-dir elsewhere already
#: stops Trino reading that directory.
_VALIDATION_PROBE = "rm -f /etc/trino/catalog/*.properties && exec /usr/lib/trino/bin/run-trino"


def _validation_container() -> DockerContainer:
    return (
        DockerContainer(TRINO_IMAGE)
        .with_exposed_ports(8080)
        .with_env("CATALOG_MANAGEMENT", "dynamic")
        .with_command(f"sh -c '{_VALIDATION_PROBE}'")
        .waiting_for(
            LogMessageWaitStrategy(
                re.compile(r"======== SERVER STARTED ========")
            ).with_startup_timeout(180)
        )
    )


def _trino_container() -> DockerContainer:
    return (
        DockerContainer(TRINO_IMAGE)
        .with_exposed_ports(8080)
        .with_command(f"sh -c '{_DYNAMIC_CATALOGS}'")
        .waiting_for(
            LogMessageWaitStrategy(
                re.compile(r"======== SERVER STARTED ========")
            ).with_startup_timeout(180)
        )
    )


class FakeKubernetes:
    """Stands in for the Kubernetes adapter in tier 1.

    Records what was written so tests can assert on it through observable
    behaviour rather than by reaching into the pipeline.

    The ephemeral validation pod is stood in for by a second Trino container. A pod
    "created" here hands back that container's endpoint, and deleting the pod puts
    the container back the way it was -- because a real pod is destroyed with every
    catalog Validation created in it.
    """

    def __init__(self, validation: DockerContainer | None = None) -> None:
        self.secrets: dict[str, dict[str, str]] = {}
        self.replicas: dict[str, int] = {}
        self.image = TRINO_IMAGE
        self.pods: dict[str, dict[str, Any]] = {}
        #: Every pod ever created, so a test can prove one was and that it went away.
        self.pod_history: list[str] = []
        #: Set by a test to make the pod report a condition waiting cannot fix.
        self.pod_problem: str | None = None
        #: Set by a test to make the pod never reach serving, proving the timeout.
        self.pod_never_serves = False
        #: Set by a test to force exactly one Verification failure, at the worker
        #: check. One-shot on purpose: it is how a test gets an Apply to fail and the
        #: Auto Rollback that follows it to succeed, which two verifications against a
        #: permanently broken cluster could never show.
        self.report_a_missing_worker_once = False
        self._validation = validation
        self._baselines: dict[str, set[str]] = {}

    async def read_secret(self, name: str) -> dict[str, str]:
        return dict(self.secrets.get(name, {}))

    async def write_secret(self, name: str, data: dict[str, str]) -> None:
        self.secrets[name] = dict(data)

    async def ready_replicas(self, deployment: str) -> int:
        ready = self.replicas.get(deployment, 0)
        if self.report_a_missing_worker_once:
            self.report_a_missing_worker_once = False
            # Kubernetes says there is one more ready worker than Trino can see, which
            # is what a worker dropping out mid-Apply looks like.
            return ready + 1
        return ready

    async def deployment_image(self, deployment: str, container: str) -> str:
        return self.image

    def attach_validation(self, container: DockerContainer) -> None:
        """Point the stand-in validation pod at a container. Until this is called a
        pod never reports serving, which is what tests of the timeout want."""
        self._validation = container

    def _probe(self) -> Trino:
        assert self._validation is not None
        return Trino(
            host=self._validation.get_container_host_ip(),
            port=int(self._validation.get_exposed_port(8080)),
        )

    async def create_pod(self, manifest: dict[str, Any]) -> None:
        name = manifest["metadata"]["name"]
        self.pods[name] = manifest
        self.pod_history.append(name)
        if self._validation is not None:
            self._baselines[name] = await self._probe().catalogs()

    async def pod_state(self, name: str) -> PodState:
        if name not in self.pods:
            return PodState(phase="Missing", problem="the pod no longer exists")
        if self.pod_problem is not None:
            return PodState(phase="Pending", problem=self.pod_problem)
        if self.pod_never_serves or self._validation is None:
            return PodState(phase="Pending")
        return PodState(
            phase="Running",
            host=self._validation.get_container_host_ip(),
            port=int(self._validation.get_exposed_port(8080)),
        )

    async def delete_pod(self, name: str) -> None:
        if self.pods.pop(name, None) is None:
            return
        baseline = self._baselines.pop(name, None)
        if baseline is None:
            return
        probe = self._probe()
        for catalog in await probe.catalogs() - baseline:
            await probe.drop_catalog(catalog)

    async def delete_pods(self, label_selector: str) -> list[str]:
        names = [
            name
            for name, manifest in self.pods.items()
            if label_selector in {f"{k}={v}" for k, v in manifest["metadata"]["labels"].items()}
        ]
        for name in names:
            await self.delete_pod(name)
        return names


@pytest.fixture(scope="session")
def mongo_container() -> Iterator[MongoDbContainer]:
    with MongoDbContainer() as container:
        yield container


@pytest.fixture(scope="session")
def trino_cluster() -> Iterator[DockerContainer]:
    """The Trino standing in for the Cluster."""
    with _trino_container() as container:
        yield container


@pytest.fixture(scope="session")
def trino_validation() -> Iterator[DockerContainer]:
    """A second Trino, standing in for the ephemeral validation coordinator.

    Validation issues CREATE CATALOG against it; pointing it at the Cluster's
    container would create the catalog for real and corrupt the test.
    """
    with _validation_container() as container:
        yield container


@pytest.fixture
def settings(mongo_container: MongoDbContainer) -> Settings:
    # A database per test. The Configuration Candidate is a singleton per Cluster,
    # so tests sharing one database share one Candidate and leak state into each
    # other -- the same shared-mutable-state problem the design accepts for
    # Operators, which tests must not inherit.
    return Settings(
        environment=Environment.TEST,
        mongo_uri=mongo_container.get_connection_url(),
        mongo_database=f"apchi_test_{uuid.uuid4().hex}",
    )


@pytest.fixture
def fake_kubernetes() -> FakeKubernetes:
    """No validation target. For tests that never run an Apply."""
    return FakeKubernetes()


@pytest.fixture
async def client(settings: Settings, fake_kubernetes: FakeKubernetes) -> AsyncIterator[AsyncClient]:
    """The primary seam, with no Trino wired.

    For tests that never reach Trino. Use `applying_client` where an Apply must run.
    """
    app = create_app(settings)
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=1)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http_client,
        app.router.lifespan_context(app),
    ):
        yield http_client


@pytest.fixture
async def applying_client(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> AsyncIterator[AsyncClient]:
    """The same seam, with two real Trinos behind it.

    MongoDB and Trino are real; only Kubernetes is faked. An Apply driven through
    here validates against one container and issues real DDL against the other --
    validating against the Cluster's own container would create the catalog for real
    and prove nothing.
    """
    fake_kubernetes.attach_validation(trino_validation)
    app = create_app(settings)
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(
        host=trino_cluster.get_container_host_ip(),
        port=int(trino_cluster.get_exposed_port(8080)),
        user=settings.trino_user,
    )
    baseline = await app.state.trino.catalogs()
    try:
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http_client,
            app.router.lifespan_context(app),
        ):
            yield http_client
    finally:
        # The coordinator outlives the test but its MongoDB does not, so a Catalog
        # left behind would collide with the next test's Apply.
        for name in await app.state.trino.catalogs() - baseline:
            await app.state.trino.drop_catalog(name)

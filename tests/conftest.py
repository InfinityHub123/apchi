"""Test harness.

Two tiers, two seams. Every test drives the HTTP API; only the Kubernetes adapter
is ever substituted. MongoDB and Trino are real in both tiers.
"""

import re
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from testcontainers.community.mongodb import MongoDbContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

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
    """

    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, str]] = {}
        self.replicas: dict[str, int] = {}

    async def read_secret(self, name: str) -> dict[str, str]:
        return dict(self.secrets.get(name, {}))

    async def write_secret(self, name: str, data: dict[str, str]) -> None:
        self.secrets[name] = dict(data)

    async def ready_replicas(self, deployment: str) -> int:
        return self.replicas.get(deployment, 0)


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
    with _trino_container() as container:
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
    settings: Settings, fake_kubernetes: FakeKubernetes, trino_cluster: DockerContainer
) -> AsyncIterator[AsyncClient]:
    """The same seam, with a real Trino behind it.

    MongoDB and Trino are real; only Kubernetes is faked. An Apply driven through
    here issues real DDL against a real coordinator.
    """
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

"""Test harness.

Two tiers, two seams. Every test drives the HTTP API; only the Kubernetes adapter
is ever substituted. MongoDB and Trino are real in both tiers.
"""

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from testcontainers.community.mongodb import MongoDbContainer
from testcontainers.community.trino import TrinoContainer

from app.config import Environment, Settings
from app.main import create_app

# Pinned to match deploy/trino-dev and the supported range in section 8, so a
# Trino upgrade breaks CI rather than production.
TRINO_IMAGE = "trinodb/trino:483"


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

    async def patch_secret(self, name: str, data: dict[str, str]) -> None:
        self.secrets.setdefault(name, {}).update(data)

    async def ready_replicas(self, deployment: str) -> int:
        return self.replicas.get(deployment, 0)


@pytest.fixture(scope="session")
def mongo_container() -> Iterator[MongoDbContainer]:
    with MongoDbContainer() as container:
        yield container


@pytest.fixture(scope="session")
def trino_cluster() -> Iterator[TrinoContainer]:
    """The Trino standing in for the Cluster."""
    with TrinoContainer(image=TRINO_IMAGE) as container:
        yield container


@pytest.fixture(scope="session")
def trino_validation() -> Iterator[TrinoContainer]:
    """A second Trino, standing in for the ephemeral validation coordinator.

    Validation issues CREATE CATALOG against it; pointing it at the Cluster's
    container would create the catalog for real and corrupt the test.
    """
    with TrinoContainer(image=TRINO_IMAGE) as container:
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
    """The primary seam. Tests drive this and nothing below it."""
    app = create_app(settings)
    app.state.kubernetes = fake_kubernetes
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http_client,
        app.router.lifespan_context(app),
    ):
        yield http_client

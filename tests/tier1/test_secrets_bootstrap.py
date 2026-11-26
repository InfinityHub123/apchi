"""Apchi writes the Secrets it owns when they are missing, and never rewrites one that exists.

The chart creates none of them: Argo CD runs a chart's Helm hooks on every sync, so a Secret
the chart created was recreated with its starting contents each time, wiping what Apchi had
applied. tests/argo proves that end to end; this proves the write itself.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.config import Settings
from app.main import create_app
from app.pipeline.applies import TERMINAL
from tests.conftest import FakeKubernetes, bootstrap_secrets

MEMORY = {"name": "scratch", "connector": "memory", "properties": {}}
GRANT = {"identity": "analyst", "catalog": "scratch", "privileges": ["SELECT"]}
OWNED = sorted(bootstrap_secrets())


async def _started(settings: Settings, fake: FakeKubernetes) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    app.state.kubernetes = fake
    app.state.trino = Trino(url="http://127.0.0.1:1")
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="https://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


async def _settled(fake: FakeKubernetes, names: list[str], timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if all(name in fake.secrets for name in names):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"never written: {sorted(set(names) - set(fake.secrets))}")


async def _apply(client: AsyncClient, timeout: float = 180.0) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return dict(record)
        await asyncio.sleep(0.05)
    raise AssertionError("Apply never settled")


def test_it_owns_all_eight() -> None:
    """The five the chart used to create as hooks, Trino's own files for each role, and the raw
    files. The Event Listener's is no longer one: the listener is in the coordinator's Server
    Configuration (#269). A ninth would need its own reason to be the chart's."""
    assert OWNED == [
        "trino-access-control",
        "trino-admin-raw-files",
        "trino-catalog-seed",
        "trino-client-certificates",
        "trino-resource-groups",
        "trino-server-config-coordinator",
        "trino-server-config-worker",
        "trino-user-mapping",
    ]


async def test_a_new_install_gets_every_one(settings: Settings) -> None:
    """Nothing exists: the coordinator waits in ContainerCreating until these appear."""
    fake = FakeKubernetes()
    fake.secrets.clear()

    async for _ in _started(settings, fake):
        await _settled(fake, OWNED)

    assert fake.secrets == bootstrap_secrets()


@pytest.mark.parametrize("name", OWNED)
async def test_one_that_exists_is_never_rewritten(settings: Settings, name: str) -> None:
    """Its content is what the running pods started with. Restarting Apchi must never change
    the Cluster: rewriting is an Apply's."""
    fake = FakeKubernetes()
    held = {"held": "what the pods were started with\n"}
    fake.secrets[name] = held

    async for _ in _started(settings, fake):
        await asyncio.sleep(0.3)

    assert fake.secrets[name] == held


async def test_one_deleted_after_an_apply_comes_back_as_applied(
    settings: Settings, fake_kubernetes: FakeKubernetes, applying_client: AsyncClient
) -> None:
    """Not the chart's starting contents: a seed deleted on a Cluster with catalogs would
    otherwise restart the coordinator with none, and deleted rules would drop every grant."""
    assert (await applying_client.post("/api/v1/catalogs", json=MEMORY)).status_code == 201
    assert (await applying_client.post("/api/v1/permissions", json=GRANT)).status_code == 201
    record = await _apply(applying_client)
    assert record["stage"] == "succeeded", record
    applied = {
        name: fake_kubernetes.secrets[name]
        for name in ("trino-catalog-seed", "trino-access-control")
    }
    for name in applied:
        del fake_kubernetes.secrets[name]

    async for _ in _started(settings, fake_kubernetes):
        await _settled(fake_kubernetes, list(applied))

    for name, before in applied.items():
        assert fake_kubernetes.secrets[name] == before, name
    assert "scratch.properties" in fake_kubernetes.secrets["trino-catalog-seed"]
    assert "analyst" in fake_kubernetes.secrets["trino-access-control"]["rules.json"]

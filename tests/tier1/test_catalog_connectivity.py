"""A Catalog that cannot connect is reported, and does not fail the Apply (#171).

`CREATE CATALOG` proves nothing about reachability. Verified against Trino 483: a catalog
pointed at a host that does not resolve is created without complaint and fails only when
something reads from it. §6 has always said so; this is the check that closes it.

The default is advisory, and the reason is in §6 too, one paragraph away: the probe pod's
network is not the coordinator's, so an unreachable catalog is not reliable evidence that
the configuration is wrong. Both halves are tested here, because the setting is the whole
design and a feature whose default path is untested is a feature with one tested path.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from testcontainers.core.container import DockerContainer

from app.adapters.trino import Trino
from app.config import Settings
from app.main import create_app
from app.pipeline.applies import TERMINAL
from tests.conftest import FakeKubernetes

CATALOGS = "/api/v1/catalogs"

#: A real connector, accepted at request time and by `CREATE CATALOG`, pointed at a host
#: that does not exist. `postgresql` is curated, so this also proves the properties are fine
#: -- what fails is the connection and nothing else.
UNREACHABLE: dict[str, Any] = {
    "name": "finance_pg",
    "connector": "postgresql",
    "properties": {
        "connection-url": "jdbc:postgresql://nosuchhost.invalid:5432/ledger",
        "connection-user": "trino_reader",
        "connection-password": "irrelevant",
    },
}

#: Reachable by construction: `memory` holds its data in the coordinator, so there is no
#: network between the probe and it.
REACHABLE: dict[str, Any] = {"name": "scratch", "connector": "memory", "properties": {}}


@pytest.fixture
async def blocking_client(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> AsyncIterator[AsyncClient]:
    """`applying_client`, with `catalog_connectivity_blocks_apply` on.

    Built here rather than parametrising the shared fixture: the setting changes what a
    failed connection *means*, so the two belong in separate tests with separate names
    rather than one test reading a flag.
    """
    fake_kubernetes.attach_validation(trino_validation)
    fake_kubernetes.attach_cluster(trino_cluster)
    app = create_app(settings.model_copy(update={"catalog_connectivity_blocks_apply": True}))
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(
        host=trino_cluster.get_container_host_ip(),
        port=int(trino_cluster.get_exposed_port(8080)),
        user=settings.trino_user,
    )
    baseline = await app.state.trino.catalogs()
    try:
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as client,
            app.router.lifespan_context(app),
        ):
            yield client
    finally:
        # The coordinator outlives the test but its MongoDB does not, so a Catalog left
        # behind would collide with the next test's Apply.
        for name in await app.state.trino.catalogs() - baseline:
            await app.state.trino.drop_catalog(name)


async def _validation(client: AsyncClient, timeout: float = 90.0) -> dict:
    started = await client.post("/api/v1/validations")
    assert started.status_code == 202, started.text
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/validations/{started.json()['id']}")).json()
        if record["outcome"] != "running":
            return record
        await asyncio.sleep(0.05)
    raise AssertionError("Validation never finished")


async def _apply(client: AsyncClient, timeout: float = 120.0) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.text
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


async def test_an_unreachable_catalog_is_a_warning_not_a_failure(
    applying_client: AsyncClient,
) -> None:
    """The default. The Candidate is fine, so the Validation passes -- and says why anyway."""
    await applying_client.post(CATALOGS, json=UNREACHABLE)

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "passed", verdict
    assert verdict["failures"] == []
    assert [(w["section"], w["resource"]) for w in verdict["warnings"]] == [
        ("catalogs", "finance_pg")
    ]


async def test_the_warning_carries_trinos_own_words(applying_client: AsyncClient) -> None:
    """Not a paraphrase. "The connection attempt failed" tells whoever has to fix this more
    than anything Apchi could write about it, and the distinction between a refused
    connection and a bad password lives entirely in that string."""
    await applying_client.post(CATALOGS, json=UNREACHABLE)

    verdict = await _validation(applying_client)

    assert "Error listing schemas for catalog finance_pg" in verdict["warnings"][0]["reason"]


async def test_a_reachable_catalog_warns_about_nothing(applying_client: AsyncClient) -> None:
    """The other half: a check that always fires is a check nobody reads."""
    await applying_client.post(CATALOGS, json=REACHABLE)

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "passed", verdict
    assert verdict["warnings"] == []


async def test_an_apply_succeeds_and_keeps_the_warning(applying_client: AsyncClient) -> None:
    """An Apply is what this must not break. The catalog is created on the Cluster, the
    Snapshot is committed, and the warning rides on the Apply record so an Operator sees it
    without having to run a separate Validation."""
    await applying_client.post(CATALOGS, json=UNREACHABLE)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert [w["resource"] for w in record["warnings"]] == ["finance_pg"]


async def test_the_setting_makes_it_block(blocking_client: AsyncClient) -> None:
    """`catalog_connectivity_blocks_apply` moves the same finding into `failures`.

    For a deployment where the probe and the coordinator are known to share a network, so
    an unreachable catalog really is a broken configuration.
    """
    await blocking_client.post(CATALOGS, json=UNREACHABLE)

    verdict = await _validation(blocking_client)

    assert verdict["outcome"] == "failed", verdict
    assert verdict["warnings"] == []
    assert [(f["section"], f["resource"]) for f in verdict["failures"]] == [
        ("catalogs", "finance_pg")
    ]


async def test_blocking_leaves_the_cluster_untouched(blocking_client: AsyncClient) -> None:
    """Validation runs before anything is written, so the refusal costs nothing: no catalog
    on the Cluster and no Snapshot."""
    await blocking_client.post(CATALOGS, json=UNREACHABLE)

    record = await _apply(blocking_client)

    assert record["stage"] == "failed", record
    assert (await blocking_client.get("/api/v1/snapshots")).json()["items"] == []

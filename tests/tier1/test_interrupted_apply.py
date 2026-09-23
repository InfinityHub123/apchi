"""An Apply interrupted by an Apchi restart.

Unfreezing the Candidate was never the whole job. Apply writes the catalog Secret
before it issues DDL, so a crash in that window leaves the durable copy naming a
catalog Trino never got -- and it arrives at the next pod restart with nothing linking
it to the Apply that caused it. Recovery therefore puts the Cluster back, once.

A restart is simulated the way it actually looks: the runner's task is cancelled
mid-Apply, then a fresh app is built over the same MongoDB and its startup runs
recovery. Same database, same cluster, different process.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from testcontainers.core.container import DockerContainer

from app.adapters.trino import Trino
from app.config import Settings
from app.main import create_app
from app.pipeline.applies import TERMINAL
from app.pipeline.auto_rollback import INCIDENT_MESSAGE
from tests.conftest import FakeKubernetes

KEPT = {"name": "kept", "connector": "memory", "properties": {}}
ADDED = {"name": "added", "connector": "memory", "properties": {}}
SECRET = "trino-catalog-seed"


@pytest.fixture(autouse=True)
async def clean_cluster(trino_cluster: DockerContainer) -> AsyncIterator[None]:
    """Per test, not per Apchi run.

    These tests enter and leave Apchi several times each -- that is what a restart is --
    so cleanup cannot live in the app fixture: it would drop the catalogs recovery is
    supposed to have left in place.
    """
    cluster = Trino(
        host=trino_cluster.get_container_host_ip(),
        port=int(trino_cluster.get_exposed_port(8080)),
    )
    baseline = await cluster.catalogs()
    try:
        yield
    finally:
        for name in await cluster.catalogs() - baseline:
            await cluster.drop_catalog(name)


@asynccontextmanager
async def _apchi(
    settings: Settings,
    kubernetes: FakeKubernetes,
    cluster: DockerContainer,
    validation: DockerContainer,
) -> AsyncIterator[AsyncClient]:
    """One run of Apchi. Leaving and re-entering this is a restart."""
    kubernetes.attach_validation(validation)
    app = create_app(settings)
    app.state.kubernetes = kubernetes
    app.state.trino = Trino(
        host=cluster.get_container_host_ip(),
        port=int(cluster.get_exposed_port(8080)),
        user=settings.trino_user,
    )
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as client,
        app.router.lifespan_context(app),
    ):
        client.app_under_test = app  # type: ignore[attr-defined]
        yield client


async def _settled(client: AsyncClient, apply_id: str, timeout: float = 60.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


async def _succeed(client: AsyncClient) -> dict:
    started = await client.post("/api/v1/applies")
    record = await _settled(client, started.json()["id"])
    assert record["stage"] == "succeeded", record.get("failure_reason")
    return record


async def _kill_mid_apply(client: AsyncClient, kubernetes: FakeKubernetes, key: str) -> str:
    """Start an Apply and cancel it once the Secret names the new catalog.

    That is the window that matters: the durable copy has been written and the DDL may
    or may not have run. Recovery has to converge either way.
    """
    started = await client.post("/api/v1/applies")
    apply_id = started.json()["id"]
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        if key in kubernetes.secrets.get(SECRET, {}):
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("the Apply never wrote the Secret")
    await client.app_under_test.state.apply_runner.shutdown()  # type: ignore[attr-defined]
    return apply_id


async def test_the_cluster_is_restored_after_a_restart_mid_apply(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as first:
        await first.post("/api/v1/catalogs", json=KEPT)
        await _succeed(first)
        await first.post("/api/v1/catalogs", json=ADDED)
        apply_id = await _kill_mid_apply(first, fake_kubernetes, "added.properties")

    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as restarted:
        record = (await restarted.get(f"/api/v1/applies/{apply_id}")).json()

    assert record["interrupted"] is True
    assert record["rollback"] == "succeeded"
    cluster = Trino(
        host=trino_cluster.get_container_host_ip(),
        port=int(trino_cluster.get_exposed_port(8080)),
    )
    live = await cluster.catalogs()
    assert "kept" in live
    assert "added" not in live


async def test_the_secret_is_restored_after_a_restart_mid_apply(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """The divergence that made this worth building: the Secret was written before the
    DDL, so it names a catalog that may never have been created."""
    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as first:
        await first.post("/api/v1/catalogs", json=KEPT)
        await _succeed(first)
        await first.post("/api/v1/catalogs", json=ADDED)
        await _kill_mid_apply(first, fake_kubernetes, "added.properties")
        assert "added.properties" in fake_kubernetes.secrets[SECRET], "precondition"

    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation):
        pass

    assert set(fake_kubernetes.secrets[SECRET]) == {"kept.properties"}


async def test_the_record_says_it_was_interrupted_not_merely_failed(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """An Operator seeing a failed Apply should be able to tell a broken configuration
    from a restart that happened to land mid-Apply."""
    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as first:
        await first.post("/api/v1/catalogs", json=ADDED)
        apply_id = await _kill_mid_apply(first, fake_kubernetes, "added.properties")

    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as restarted:
        record = (await restarted.get(f"/api/v1/applies/{apply_id}")).json()

    assert record["interrupted"] is True
    assert "Apchi restarted" in record["failure_reason"]


async def test_the_candidate_is_unfrozen_after_recovery(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """The part that must never be left to chance: a restart mid-Apply must not freeze
    the Candidate permanently. Resolved before the Cluster is touched, so even a
    rollback that fails cannot leave it frozen."""
    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as first:
        await first.post("/api/v1/catalogs", json=ADDED)
        await _kill_mid_apply(first, fake_kubernetes, "added.properties")

    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as restarted:
        edit = await restarted.post(
            "/api/v1/catalogs", json={"name": "after", "connector": "memory", "properties": {}}
        )

    assert edit.status_code == 201


async def test_recovery_creates_no_snapshot(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as first:
        await first.post("/api/v1/catalogs", json=KEPT)
        await _succeed(first)
        await first.post("/api/v1/catalogs", json=ADDED)
        await _kill_mid_apply(first, fake_kubernetes, "added.properties")

    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as restarted:
        snapshots = (await restarted.get("/api/v1/snapshots")).json()

    assert [snapshot["number"] for snapshot in snapshots] == [1]


async def test_a_failed_recovery_engages_maintenance_mode(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """One attempt, no retry. A restart that cannot put the Cluster back is the same
    incident a failed Auto Rollback is."""
    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as first:
        await first.post("/api/v1/catalogs", json=ADDED)
        apply_id = await _kill_mid_apply(first, fake_kubernetes, "added.properties")

    broken = settings.model_copy(update={"verification_catalog": "no_such_catalog"})
    async with _apchi(broken, fake_kubernetes, trino_cluster, trino_validation) as restarted:
        record = (await restarted.get(f"/api/v1/applies/{apply_id}")).json()
        state = (await restarted.get("/api/v1/admin/maintenance-mode")).json()
        refused = await restarted.post("/api/v1/catalogs", json=KEPT)

    assert record["stage"] == "incident"
    assert record["rollback"] == "failed"
    assert record["operator_message"] == INCIDENT_MESSAGE
    assert state["engaged"] is True
    assert state["engaged_by"] == "auto_rollback_failure"
    assert refused.status_code == 409


async def test_an_apply_interrupted_during_validation_is_not_rolled_back(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """Nothing had reached the Cluster, so there is nothing to undo -- and a rollback
    would be work against a Cluster that is fine."""
    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as first:
        await first.post("/api/v1/catalogs", json=ADDED)
        started = await first.post("/api/v1/applies")
        apply_id = started.json()["id"]
        await first.app_under_test.state.apply_runner.shutdown()  # type: ignore[attr-defined]

    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as restarted:
        record = (await restarted.get(f"/api/v1/applies/{apply_id}")).json()

    assert record["interrupted"] is True
    assert record["rollback"] is None
    assert record["stage"] == "failed"

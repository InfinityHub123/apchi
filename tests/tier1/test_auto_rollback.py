"""Auto Rollback, and the incident state when it cannot help.

Failures are forced the way they happen. Verification is what decides an Apply went
wrong, so these tests make Verification fail for a real reason and watch what Apchi
does to the Cluster afterwards.

Two different forcings, because the two outcomes need different worlds. A rollback
that *succeeds* needs Verification to fail once and then pass, which is a worker
dropping out mid-Apply. A rollback that *fails* needs a Cluster that cannot verify at
all, which is the smoke query having nowhere to run.
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


async def _settled(client: AsyncClient, apply_id: str, timeout: float = 60.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


async def _apply(client: AsyncClient) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    return await _settled(client, started.json()["id"])


def _cluster(container: DockerContainer) -> Trino:
    return Trino(host=container.get_container_host_ip(), port=int(container.get_exposed_port(8080)))


@asynccontextmanager
async def _unverifiable(
    settings: Settings,
    kubernetes: FakeKubernetes,
    cluster: DockerContainer,
    validation: DockerContainer,
) -> AsyncIterator[AsyncClient]:
    """A client whose Verification can never pass: the smoke query runs against a
    catalog that does not exist. Both the Apply's Verification and the Auto Rollback's
    fail, which is the incident."""
    kubernetes.attach_validation(validation)
    broken = settings.model_copy(update={"verification_catalog": "no_such_catalog"})
    app = create_app(broken)
    app.state.kubernetes = kubernetes
    app.state.trino = Trino(
        host=cluster.get_container_host_ip(),
        port=int(cluster.get_exposed_port(8080)),
        user=broken.trino_user,
    )
    baseline = await app.state.trino.catalogs()
    try:
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as client,
            app.router.lifespan_context(app),
        ):
            yield client
    finally:
        # An Apply that ends in an incident makes no promise about what it left behind,
        # and the coordinator is shared with every other test.
        for name in await app.state.trino.catalogs() - baseline:
            await app.state.trino.drop_catalog(name)


@pytest.fixture
async def snapshot_one(applying_client: AsyncClient) -> AsyncClient:
    """A Cluster with one committed Snapshot, which is what Auto Rollback goes back
    to."""
    await applying_client.post("/api/v1/catalogs", json=KEPT)
    record = await _apply(applying_client)
    assert record["stage"] == "succeeded", record.get("failure_reason")
    return applying_client


@pytest.fixture
async def rolled_back(snapshot_one: AsyncClient, fake_kubernetes: FakeKubernetes) -> dict:
    """An Apply that created a catalog, failed Verification once, and was rolled
    back."""
    fake_kubernetes.report_a_missing_worker_once = True
    await snapshot_one.post("/api/v1/catalogs", json=ADDED)
    return await _apply(snapshot_one)


async def test_a_verification_failure_returns_the_cluster_to_its_latest_snapshot(
    rolled_back: dict, trino_cluster: DockerContainer
) -> None:
    """The catalog this Apply created is dropped again; the one the Snapshot holds is
    left alone."""
    assert rolled_back["stage"] == "failed"
    assert rolled_back["rollback"] == "succeeded"

    live = await _cluster(trino_cluster).catalogs()

    assert "kept" in live
    assert "added" not in live


async def test_auto_rollback_restores_the_secret_too(
    rolled_back: dict, fake_kubernetes: FakeKubernetes
) -> None:
    """Restoring only the live catalogs would leave the durable copy holding one the
    Snapshot never had, to be seeded back in at the next pod restart."""
    assert set(fake_kubernetes.secrets[SECRET]) == {"kept.properties"}


async def test_auto_rollback_creates_no_snapshot(
    rolled_back: dict, snapshot_one: AsyncClient
) -> None:
    """The latest-Snapshot pointer never moves. Auto Rollback is not a Full Rollback."""
    snapshots = (await snapshot_one.get("/api/v1/snapshots")).json()

    assert [snapshot["number"] for snapshot in snapshots] == [1]


async def test_the_candidate_is_left_alone_so_the_operator_can_fix_it(
    rolled_back: dict, snapshot_one: AsyncClient
) -> None:
    """Auto Rollback restores the Cluster, not the Candidate. Discarding the edit would
    throw away the Operator's work along with the failure."""
    staged = [catalog["name"] for catalog in (await snapshot_one.get("/api/v1/catalogs")).json()]

    assert staged == ["added", "kept"]


async def test_the_record_carries_the_failure_reason(rolled_back: dict) -> None:
    """An Operator has to be able to diagnose without log access."""
    assert "VerificationFailed" in rolled_back["failure_reason"]
    assert "workers have registered" in rolled_back["failure_reason"]


async def test_the_rollback_appears_in_the_stage_history(rolled_back: dict) -> None:
    """So a client watching the stream sees the recovery, not a silent gap."""
    assert [event["stage"] for event in rolled_back["history"]] == [
        "validating",
        "applying",
        "verifying",
        "rolling_back",
        "failed",
    ]


async def test_the_rollback_stage_names_what_went_wrong(rolled_back: dict) -> None:
    rolling_back = next(e for e in rolled_back["history"] if e["stage"] == "rolling_back")

    assert "workers have registered" in rolling_back["detail"]


async def test_a_validation_failure_attempts_no_rollback(applying_client: AsyncClient) -> None:
    """Nothing reached the Cluster, so there is nothing to undo."""
    await applying_client.post(
        "/api/v1/catalogs", json={"name": "imaginary", "connector": "nosuchconnector"}
    )

    record = await _apply(applying_client)

    assert record["stage"] == "failed"
    assert record["rollback"] is None
    assert "rolling_back" not in [event["stage"] for event in record["history"]]


async def test_a_dropped_catalog_is_restored_by_the_rollback(
    snapshot_one: AsyncClient, fake_kubernetes: FakeKubernetes, trino_cluster: DockerContainer
) -> None:
    """The other half of the compensating DDL. A missing catalog breaks every query
    against it, so getting this one back matters more than removing an extra."""
    fake_kubernetes.report_a_missing_worker_once = True
    await snapshot_one.delete("/api/v1/catalogs/kept")

    record = await _apply(snapshot_one)

    assert record["rollback"] == "succeeded"
    assert "kept" in await _cluster(trino_cluster).catalogs()
    assert set(fake_kubernetes.secrets[SECRET]) == {"kept.properties"}


async def test_a_failed_rollback_ends_in_an_incident(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """Two consecutive verification failures mean the problem is not the configuration.
    Apchi stops touching the Cluster and says so."""
    async with _unverifiable(settings, fake_kubernetes, trino_cluster, trino_validation) as client:
        await client.post("/api/v1/catalogs", json=ADDED)

        record = await _apply(client)

    assert record["stage"] == "incident"
    assert record["rollback"] == "failed"
    assert record["operator_message"] == INCIDENT_MESSAGE
    assert "Auto Rollback then failed" in record["failure_reason"]


async def test_an_incident_engages_maintenance_mode(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """Nobody edits a Cluster whose state nobody has described."""
    async with _unverifiable(settings, fake_kubernetes, trino_cluster, trino_validation) as client:
        await client.post("/api/v1/catalogs", json=ADDED)
        await _apply(client)

        state = (await client.get("/api/v1/admin/maintenance-mode")).json()

    assert state["engaged"] is True
    assert state["engaged_by"] == "auto_rollback_failure"


async def test_operator_mutations_are_rejected_during_an_incident(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    async with _unverifiable(settings, fake_kubernetes, trino_cluster, trino_validation) as client:
        await client.post("/api/v1/catalogs", json=ADDED)
        await _apply(client)

        refused = await client.post("/api/v1/catalogs", json=KEPT)
        second_apply = await client.post("/api/v1/applies")

    assert refused.status_code == 409
    assert refused.json()["code"] == "maintenance_mode"
    assert second_apply.status_code == 409


async def test_reads_continue_during_an_incident(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """Operators keep read access: diagnosing is exactly what they need to do."""
    async with _unverifiable(settings, fake_kubernetes, trino_cluster, trino_validation) as client:
        await client.post("/api/v1/catalogs", json=ADDED)
        await _apply(client)

        catalogs = await client.get("/api/v1/catalogs")
        review = await client.get("/api/v1/review")
        applies = await client.get("/api/v1/applies")

    assert catalogs.status_code == 200
    assert review.status_code == 200
    assert applies.status_code == 200


async def test_maintenance_mode_survives_a_restart(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """An incident must not be cleared by Apchi restarting. That is why the state is
    persisted rather than held in memory."""
    async with _unverifiable(settings, fake_kubernetes, trino_cluster, trino_validation) as client:
        await client.post("/api/v1/catalogs", json=ADDED)
        await _apply(client)

    async with _unverifiable(
        settings, fake_kubernetes, trino_cluster, trino_validation
    ) as restarted:
        state = (await restarted.get("/api/v1/admin/maintenance-mode")).json()

    assert state["engaged"] is True


async def test_an_admin_can_engage_and_release_maintenance_mode(client: AsyncClient) -> None:
    """Releasing is how an incident is closed, so it has to work while engaged."""
    engaged = await client.put(
        "/api/v1/admin/maintenance-mode", json={"engaged": True, "reason": "Trino upgrade"}
    )
    refused = await client.post("/api/v1/catalogs", json=KEPT)

    released = await client.put("/api/v1/admin/maintenance-mode", json={"engaged": False})
    allowed = await client.post("/api/v1/catalogs", json=KEPT)

    assert engaged.json()["engaged_by"] == "admin"
    assert refused.status_code == 409
    assert "Trino upgrade" in refused.json()["message"]
    assert released.json()["engaged"] is False
    assert allowed.status_code == 201


async def test_an_undeliverable_alert_does_not_mask_the_incident(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """The alert goes to a system that may itself be down. Maintenance Mode engages
    first and does not wait on it."""
    unreachable = settings.model_copy(update={"alert_webhook_url": "http://127.0.0.1:9/hook"})
    async with _unverifiable(
        unreachable, fake_kubernetes, trino_cluster, trino_validation
    ) as client:
        await client.post("/api/v1/catalogs", json=ADDED)

        record = await _apply(client)
        state = (await client.get("/api/v1/admin/maintenance-mode")).json()

    assert record["stage"] == "incident"
    assert state["engaged"] is True

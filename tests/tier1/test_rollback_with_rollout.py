"""Auto Rollback for a Section Trino adopts only by restarting.

Undoing a rollout-required change means restarting again, and that restart is part of the
single bounded attempt rather than a retry. Unlike Catalogs there is no compensating
statement to work out -- the file engines are declarative, so the previous content is simply
written again -- but the Cluster does not adopt it until the coordinator comes back.
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
from app.sections.event_listeners.generator import FILE_KEY
from tests.conftest import FakeKubernetes

LISTENER_SECRET = "trino-event-listener"
VOLUME = "apchi-event-listener"
KEPT = {"name": "kept", "connector": "memory", "properties": {}}
ADDED = {"name": "added", "connector": "memory", "properties": {}}
FIRST = {
    "name": "audit",
    "type": "http",
    "properties": {"http-event-listener.connect-ingest-uri": "http://first:8080/e"},
}
SECOND_URI = "http://second:9090/e"


async def _settled(client: AsyncClient, apply_id: str, timeout: float = 120.0) -> dict:
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


@asynccontextmanager
async def _running(
    settings: Settings,
    kubernetes: FakeKubernetes,
    cluster: DockerContainer,
    validation: DockerContainer,
) -> AsyncIterator[AsyncClient]:
    kubernetes.attach_validation(validation)
    kubernetes.attach_cluster(cluster)
    app = create_app(settings)
    app.state.kubernetes = kubernetes
    app.state.trino = Trino(
        host=cluster.get_container_host_ip(),
        port=int(cluster.get_exposed_port(8080)),
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
        for name in await app.state.trino.catalogs() - baseline:
            await app.state.trino.drop_catalog(name)


@pytest.fixture
async def snapshot_one(applying_client: AsyncClient) -> AsyncClient:
    """A Cluster running one Catalog and one Event Listener, verified and committed."""
    await applying_client.post("/api/v1/catalogs", json=KEPT)
    await applying_client.post("/api/v1/event-listeners", json=FIRST)
    record = await _apply(applying_client)
    assert record["stage"] == "succeeded", record.get("failure_reason")
    return applying_client


async def test_a_failure_after_the_rollout_restores_the_listener_and_restarts_again(
    snapshot_one: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    fake_kubernetes.restarts.clear()
    fake_kubernetes.report_a_missing_worker_once = True
    await snapshot_one.patch(
        "/api/v1/event-listeners/audit",
        json={"properties": {"http-event-listener.connect-ingest-uri": SECOND_URI}},
    )

    record = await _apply(snapshot_one)

    assert record["stage"] == "failed"
    assert record["rollback"] == "succeeded"
    restored = fake_kubernetes.secrets[LISTENER_SECRET][FILE_KEY]
    assert "http://first:8080/e" in restored, "the Snapshot's listener is back"
    assert SECOND_URI not in restored
    assert len(fake_kubernetes.restarts) == 2, "one to apply the change, one to undo it"


async def test_both_sections_are_restored_together_in_one_attempt(
    snapshot_one: AsyncClient, fake_kubernetes: FakeKubernetes, trino_cluster: DockerContainer
) -> None:
    """A Candidate that changed both is undone in one pass, not one rollback per Section."""
    fake_kubernetes.restarts.clear()
    fake_kubernetes.report_a_missing_worker_once = True
    await snapshot_one.post("/api/v1/catalogs", json=ADDED)
    await snapshot_one.patch(
        "/api/v1/event-listeners/audit",
        json={"properties": {"http-event-listener.connect-ingest-uri": SECOND_URI}},
    )

    record = await _apply(snapshot_one)

    assert record["rollback"] == "succeeded", record.get("failure_reason")
    live = await Trino(
        host=trino_cluster.get_container_host_ip(),
        port=int(trino_cluster.get_exposed_port(8080)),
    ).catalogs()
    assert "kept" in live
    assert "added" not in live
    assert "http://first:8080/e" in fake_kubernetes.secrets[LISTENER_SECRET][FILE_KEY]
    assert len(fake_kubernetes.restarts) == 2


async def test_the_rollback_creates_no_snapshot(
    snapshot_one: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    fake_kubernetes.report_a_missing_worker_once = True
    await snapshot_one.patch(
        "/api/v1/event-listeners/audit",
        json={"properties": {"http-event-listener.connect-ingest-uri": SECOND_URI}},
    )

    await _apply(snapshot_one)

    snapshots = (await snapshot_one.get("/api/v1/snapshots")).json()
    assert [snapshot["number"] for snapshot in snapshots] == [1]


async def test_a_rollback_with_no_listener_change_does_not_restart(
    snapshot_one: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The Cluster has a listener, and it is not what went wrong. Restarting to undo a
    catalog would cost every running query for nothing."""
    fake_kubernetes.restarts.clear()
    fake_kubernetes.report_a_missing_worker_once = True
    await snapshot_one.post("/api/v1/catalogs", json=ADDED)

    record = await _apply(snapshot_one)

    assert record["rollback"] == "succeeded", record.get("failure_reason")
    assert fake_kubernetes.restarts == []


async def test_removing_a_listener_is_undone_by_putting_it_back(
    snapshot_one: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The mount is the meaning: undoing a removal has to remount, or the coordinator comes
    back with no listener at all."""
    fake_kubernetes.report_a_missing_worker_once = True
    await snapshot_one.delete("/api/v1/event-listeners/audit")

    record = await _apply(snapshot_one)

    assert record["rollback"] == "succeeded", record.get("failure_reason")
    assert VOLUME in fake_kubernetes.mounts
    assert "http://first:8080/e" in fake_kubernetes.secrets[LISTENER_SECRET][FILE_KEY]


async def test_a_rollback_whose_own_rollout_stalls_ends_in_an_incident(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """One attempt, no retry. The Apply's own rollout finished; the rollback's did not, and
    Apchi stops touching a Cluster whose state nobody has described."""
    quick = settings.model_copy(update={"rollout_timeout_seconds": 1.0})

    async with _running(quick, fake_kubernetes, trino_cluster, trino_validation) as client:
        await client.post("/api/v1/event-listeners", json=FIRST)
        # The Apply's rollout is the first; every one after it stalls.
        fake_kubernetes.stall_rollout_from = 2
        fake_kubernetes.report_a_missing_worker_once = True

        record = await _apply(client)

        state = (await client.get("/api/v1/admin/maintenance-mode")).json()
        refused = await client.post("/api/v1/catalogs", json=KEPT)

    assert record["stage"] == "incident"
    assert record["rollback"] == "failed"
    assert record["operator_message"] == INCIDENT_MESSAGE
    assert "did not finish rolling out" in record["failure_reason"]
    assert state["engaged"] is True
    assert refused.status_code == 409
    assert len(fake_kubernetes.restarts) == 2, "one attempt at undoing it, and no retry"

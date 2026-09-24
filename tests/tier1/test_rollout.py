"""The rollout Apply engine: delivering configuration Trino adopts only by restarting.

Catalogs proved the Candidate machinery without ever restarting Trino. This is the other
half, and the part three more Sections depend on. What matters is that Apply waits for the
coordinator to come back before Verification asks it anything, that a coordinator which
never comes back fails the Apply instead of hanging it, and that an Apply which needed no
restart does not perform one.
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
from app.sections.event_listeners.generator import FILE_KEY, MOUNT_PATH
from tests.conftest import FakeKubernetes

LISTENER_SECRET = "trino-event-listener"
VOLUME = "apchi-event-listener"
CATALOG = {"name": "scratch", "connector": "memory", "properties": {}}
HTTP = {
    "name": "audit",
    "type": "http",
    "properties": {"http-event-listener.connect-ingest-uri": "http://collector:8080/events"},
}


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


async def test_applying_a_listener_delivers_the_file_and_rolls_out(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/event-listeners", json=HTTP)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    delivered = fake_kubernetes.secrets[LISTENER_SECRET][FILE_KEY]
    assert "event-listener.name=http" in delivered
    assert "http-event-listener.connect-ingest-uri=http://collector:8080/events" in delivered
    assert len(fake_kubernetes.restarts) == 1


async def test_the_listener_file_is_mounted_when_one_is_configured(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Trino refuses to start if a file it was told to read is missing, so the presence of
    the mount is what says there is a listener at all."""
    await applying_client.post("/api/v1/event-listeners", json=HTTP)

    await _apply(applying_client)

    assert fake_kubernetes.mounts[VOLUME] == {
        "secret": LISTENER_SECRET,
        "path": MOUNT_PATH,
        "key": FILE_KEY,
    }


async def test_removing_the_listener_unmounts_the_file(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """A Secret with no key becomes a *directory* under a subPath mount, and Trino dies on
    it with "Is a directory". Absence has to be the mount being gone."""
    await applying_client.post("/api/v1/event-listeners", json=HTTP)
    await _apply(applying_client)

    await applying_client.delete("/api/v1/event-listeners/audit")
    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert VOLUME not in fake_kubernetes.mounts
    assert fake_kubernetes.secrets[LISTENER_SECRET] == {}


async def test_a_catalogs_only_apply_restarts_nothing(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Half the Sections reach the Cluster without a restart, and routine catalog work
    must never cost anybody their queries."""
    await applying_client.post("/api/v1/catalogs", json=CATALOG)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert fake_kubernetes.restarts == []
    assert "rolling_out" not in [event["stage"] for event in record["history"]]


async def test_a_candidate_touching_both_restarts_once(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/catalogs", json=CATALOG)
    await applying_client.post("/api/v1/event-listeners", json=HTTP)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert len(fake_kubernetes.restarts) == 1


async def test_an_unchanged_listener_restarts_nothing(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Otherwise every Apply on a Cluster that has a listener would terminate every
    running query for nothing."""
    await applying_client.post("/api/v1/event-listeners", json=HTTP)
    await _apply(applying_client)
    fake_kubernetes.restarts.clear()

    await applying_client.post("/api/v1/catalogs", json=CATALOG)
    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert fake_kubernetes.restarts == []


async def test_the_rollout_is_its_own_stage_in_the_history(
    applying_client: AsyncClient,
) -> None:
    """So a client watching the stream sees the restart rather than a long silence."""
    await applying_client.post("/api/v1/event-listeners", json=HTTP)

    record = await _apply(applying_client)

    assert [event["stage"] for event in record["history"]] == [
        "validating",
        "applying",
        "rolling_out",
        "verifying",
        "committing",
        "succeeded",
    ]


async def test_the_restart_reason_names_the_apply_and_the_section(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """An Admin looking at why the coordinator restarted should find the answer on the
    Deployment rather than in Apchi's logs."""
    await applying_client.post("/api/v1/event-listeners", json=HTTP)

    record = await _apply(applying_client)

    assert record["id"] in fake_kubernetes.restarts[0]
    assert "event_listeners" in fake_kubernetes.restarts[0]


async def test_a_verified_apply_commits_a_snapshot_holding_both_sections(
    applying_client: AsyncClient,
) -> None:
    await applying_client.post("/api/v1/catalogs", json=CATALOG)
    await applying_client.post("/api/v1/event-listeners", json=HTTP)

    record = await _apply(applying_client)

    snapshot = (await applying_client.get(f"/api/v1/snapshots/{record['snapshot']}")).json()
    assert list(snapshot["sections"]["catalogs"]) == ["scratch"]
    assert list(snapshot["sections"]["event_listeners"]) == ["audit"]


async def test_verification_runs_through_a_coordinator_that_really_restarts(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """The behaviour the rollout engine exists to get right. The container backing the
    Cluster genuinely goes away and comes back, so Verification polls through a
    coordinator that is unreachable and then starting and then serving -- which no amount
    of recording a restart would prove.
    """
    fake_kubernetes.restarts_for_real = True

    async with _running(settings, fake_kubernetes, trino_cluster, trino_validation) as client:
        await client.post("/api/v1/event-listeners", json=HTTP)

        record = await _apply(client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert len(fake_kubernetes.restarts) == 1


async def test_a_coordinator_that_never_comes_back_fails_the_apply(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """The hard timeout. An Apply must not hang on a rollout that never finishes."""
    fake_kubernetes.rollout_never_completes = True
    quick = settings.model_copy(update={"rollout_timeout_seconds": 1.0})

    async with _running(quick, fake_kubernetes, trino_cluster, trino_validation) as client:
        await client.post("/api/v1/event-listeners", json=HTTP)

        record = await _apply(client)

    assert record["stage"] in {"failed", "incident"}
    assert "did not finish rolling out within 1s" in record["failure_reason"]
    assert "rolling_out" in [event["stage"] for event in record["history"]]


async def test_a_rollout_failure_is_rolled_back(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """A Rollout failure means the configuration reached the Cluster and the Cluster has
    not come back on it, which is what Auto Rollback is for."""
    fake_kubernetes.rollout_never_completes = True
    quick = settings.model_copy(update={"rollout_timeout_seconds": 1.0})

    async with _running(quick, fake_kubernetes, trino_cluster, trino_validation) as client:
        await client.post("/api/v1/event-listeners", json=HTTP)

        record = await _apply(client)

    assert record["rollback"] is not None, "the Cluster was touched, so a rollback is owed"
    assert (await _snapshots(settings, fake_kubernetes, trino_cluster, trino_validation)) == []


async def _snapshots(
    settings: Settings,
    kubernetes: FakeKubernetes,
    cluster: DockerContainer,
    validation: DockerContainer,
) -> list[dict]:
    async with _running(settings, kubernetes, cluster, validation) as client:
        return list((await client.get("/api/v1/snapshots")).json())


@pytest.mark.parametrize("stage", ["rolling_out"])
async def test_the_rollout_stage_is_streamed(applying_client: AsyncClient, stage: str) -> None:
    """The stream is a view over the durable record, so a client that reconnects sees the
    rollout it missed."""
    await applying_client.post("/api/v1/event-listeners", json=HTTP)
    started = await applying_client.post("/api/v1/applies")
    apply_id = started.json()["id"]
    await _settled(applying_client, apply_id)

    async with applying_client.stream("GET", f"/api/v1/applies/{apply_id}/events") as response:
        body = b"".join([chunk async for chunk in response.aiter_bytes()]).decode()

    assert f'"stage": "{stage}"' in body

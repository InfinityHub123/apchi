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

from app.adapters.kubernetes import CREDENTIAL_FILE_MODE
from app.adapters.trino import Trino
from app.config import Settings
from app.main import create_app
from app.pipeline.applies import TERMINAL
from app.sections.resource_groups.generator import MANAGER_FILE, MANAGER_KEY, MANAGER_PATH
from tests.conftest import (
    RESTART_SECRET,
    RESTART_SECTION,
    RESTART_VOLUME,
    FakeKubernetes,
    change_the_restart,
    container_url,
    stage_a_restart,
    unstage_the_restart,
)

CATALOG = {"name": "scratch", "connector": "memory", "properties": {}}


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
    app.state.trino = Trino(url=container_url(cluster), user=settings.trino_user)
    baseline = await app.state.trino.catalogs()
    try:
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="https://apchi") as client,
            app.router.lifespan_context(app),
        ):
            yield client
    finally:
        for name in await app.state.trino.catalogs() - baseline:
            await app.state.trino.drop_catalog(name)


async def test_applying_a_restart_section_delivers_the_file_and_rolls_out(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await stage_a_restart(applying_client)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert fake_kubernetes.secrets[RESTART_SECRET][MANAGER_KEY] == MANAGER_FILE
    assert len(fake_kubernetes.restarts) == 1


async def test_the_file_is_mounted_when_the_section_is_configured(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Trino refuses to start if a file it was told to read is missing, so the presence of
    the mount is what says the Section is configured at all."""
    await stage_a_restart(applying_client)

    await _apply(applying_client)

    assert fake_kubernetes.mounts[MANAGER_PATH] == {
        "volume": RESTART_VOLUME,
        "secret": RESTART_SECRET,
        "key": MANAGER_KEY,
        # Restricted only because this pod declares an fsGroup; see
        # tests/tier1/test_credential_permissions.py for why that condition exists.
        "mode": CREDENTIAL_FILE_MODE,
    }


async def test_a_mount_apchi_adds_is_on_the_pod_spec_apchi_reads(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The mount has to appear where Apchi *looks*, which is the Deployment's pod template.

    Two places record a mount and only one of them is Apchi's own view. `mounts` is what
    these tests assert against; `deployment_pod_spec` is what Apchi reads -- the §16
    preconditions. The fake used to keep only the first, so a file Apchi had just mounted was
    invisible to Apchi -- a Cluster that cannot exist, because the API server has no way to
    accept a mount and not show it.
    """
    await stage_a_restart(applying_client)

    await _apply(applying_client)

    spec = await fake_kubernetes.deployment_pod_spec("trino-coordinator")
    container = next(c for c in spec["containers"] if c["name"] == "trino")
    mounted = {mount["mountPath"]: mount["name"] for mount in container["volumeMounts"]}
    assert mounted.get(MANAGER_PATH) == RESTART_VOLUME
    assert any(volume["name"] == RESTART_VOLUME for volume in spec["volumes"]), (
        "a mount naming a volume the pod template does not declare is one the API server "
        "would reject outright"
    )


async def test_unmounting_takes_the_mount_off_the_pod_spec_too(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The other half: a stale mount left on the template would make a removed Section look
    configured to the preconditions, which is the same failure facing the other way."""
    await stage_a_restart(applying_client)
    await _apply(applying_client)

    await unstage_the_restart(applying_client)
    await _apply(applying_client)

    spec = await fake_kubernetes.deployment_pod_spec("trino-coordinator")
    container = next(c for c in spec["containers"] if c["name"] == "trino")
    assert MANAGER_PATH not in {mount["mountPath"] for mount in container["volumeMounts"]}
    assert not any(volume["name"] == RESTART_VOLUME for volume in spec["volumes"])


async def test_emptying_the_section_unmounts_the_file(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """A Secret with no key becomes a *directory* under a subPath mount, and Trino dies on
    it with "Is a directory". Absence has to be the mount being gone."""
    await stage_a_restart(applying_client)
    await _apply(applying_client)

    await unstage_the_restart(applying_client)
    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert MANAGER_PATH not in fake_kubernetes.mounts
    assert fake_kubernetes.secrets[RESTART_SECRET] == {}


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
    await stage_a_restart(applying_client)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert len(fake_kubernetes.restarts) == 1


async def test_an_unchanged_section_restarts_nothing(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Otherwise every Apply on a Cluster that has resource groups would terminate every
    running query for nothing."""
    await stage_a_restart(applying_client)
    await _apply(applying_client)
    fake_kubernetes.restarts.clear()

    await applying_client.post("/api/v1/catalogs", json=CATALOG)
    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert fake_kubernetes.restarts == []


async def test_the_record_says_whether_this_apply_restarted_trino(
    applying_client: AsyncClient,
) -> None:
    """So an Operator reading the history afterwards can tell an Apply that cost every
    running query from one that cost nothing."""
    await applying_client.post("/api/v1/catalogs", json=CATALOG)
    quiet = await _apply(applying_client)

    await stage_a_restart(applying_client)
    noisy = await _apply(applying_client)

    assert quiet["rolled_out"] is False
    assert noisy["rolled_out"] is True


async def test_emptying_the_section_is_a_change_and_restarts(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Taking the groups away has to reach Trino just as adding them did -- the file is
    read once per process, so the pod that still has it keeps using it."""
    await stage_a_restart(applying_client)
    await _apply(applying_client)
    fake_kubernetes.restarts.clear()

    await unstage_the_restart(applying_client)
    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert record["rolled_out"] is True
    assert len(fake_kubernetes.restarts) == 1


async def test_editing_a_group_in_place_restarts(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The decision is what changed, not whether the Section gained or lost a resource."""
    await stage_a_restart(applying_client)
    await _apply(applying_client)
    fake_kubernetes.restarts.clear()

    await change_the_restart(applying_client)
    record = await _apply(applying_client)

    assert record["rolled_out"] is True
    assert len(fake_kubernetes.restarts) == 1


async def test_the_rollout_is_its_own_stage_in_the_history(
    applying_client: AsyncClient,
) -> None:
    """So a client watching the stream sees the restart rather than a long silence."""
    await stage_a_restart(applying_client)

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
    await stage_a_restart(applying_client)

    record = await _apply(applying_client)

    assert record["id"] in fake_kubernetes.restarts[0]
    assert RESTART_SECTION in fake_kubernetes.restarts[0]


async def test_a_verified_apply_commits_a_snapshot_holding_both_sections(
    applying_client: AsyncClient,
) -> None:
    await applying_client.post("/api/v1/catalogs", json=CATALOG)
    await stage_a_restart(applying_client)

    record = await _apply(applying_client)

    snapshot = (await applying_client.get(f"/api/v1/snapshots/{record['snapshot']}")).json()
    assert list(snapshot["sections"]["catalogs"]) == ["scratch"]
    assert "global" in snapshot["sections"][RESTART_SECTION]


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
        await stage_a_restart(client)

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
        await stage_a_restart(client)

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
        await stage_a_restart(client)

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
        return list((await client.get("/api/v1/snapshots")).json()["items"])


@pytest.mark.parametrize("stage", ["rolling_out"])
async def test_the_rollout_stage_is_streamed(applying_client: AsyncClient, stage: str) -> None:
    """The stream is a view over the durable record, so a client that reconnects sees the
    rollout it missed."""
    await stage_a_restart(applying_client)
    started = await applying_client.post("/api/v1/applies")
    apply_id = started.json()["id"]
    await _settled(applying_client, apply_id)

    async with applying_client.stream("GET", f"/api/v1/applies/{apply_id}/events") as response:
        body = b"".join([chunk async for chunk in response.aiter_bytes()]).decode()

    assert f'"stage": "{stage}"' in body

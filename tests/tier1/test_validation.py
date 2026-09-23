"""Validation against an ephemeral Trino.

The Cluster is one container, the ephemeral coordinator another: validating against
the Cluster's own Trino would create the catalog for real and prove nothing. What
these tests care about is that a configuration Trino rejects never reaches the
Cluster, and that the pod standing in for it always goes away.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from httpx import ASGITransport, AsyncClient
from testcontainers.core.container import DockerContainer

from app.adapters.trino import Trino
from app.config import Settings
from app.main import create_app
from app.pipeline.validation import ROLE_LABEL, VALIDATION_ROLE
from tests.conftest import FakeKubernetes

MEMORY = {"name": "scratch", "connector": "memory", "properties": {}}
#: Passes static validation -- an uncurated connector passes through -- and is then
#: rejected by a real Trino, which is exactly the gap Trino validation exists to fill.
IMAGINARY = {"name": "imaginary", "connector": "nosuchconnector", "properties": {}}
SECRET = "trino-catalog-seed"


async def _settled(client: AsyncClient, apply_id: str, timeout: float = 60.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in {"succeeded", "failed"}:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


async def _apply(client: AsyncClient) -> dict:
    started = await client.post("/api/v1/applies")
    return await _settled(client, started.json()["id"])


async def _validation(client: AsyncClient, timeout: float = 60.0) -> dict:
    started = await client.post("/api/v1/validations")
    assert started.status_code == 202
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/validations/{started.json()['id']}")).json()
        if record["outcome"] != "running":
            return record
        await asyncio.sleep(0.05)
    raise AssertionError("Validation never finished")


@asynccontextmanager
async def _running(
    settings: Settings, kubernetes: FakeKubernetes, cluster: DockerContainer
) -> AsyncIterator[AsyncClient]:
    """An app with settings of the test's choosing, for the cases that need a
    different validation timeout than production would use."""
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
        yield client


async def test_a_catalog_trino_rejects_fails_the_apply(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/catalogs", json=IMAGINARY)

    record = await _apply(applying_client)

    assert record["stage"] == "failed"
    assert "imaginary" in record["failure_reason"]


async def test_a_validation_failure_leaves_the_cluster_untouched(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, trino_cluster: DockerContainer
) -> None:
    """Validation runs before anything is touched, so a failure costs the Operator
    nothing: no catalog on the Cluster, and no Secret written."""
    await applying_client.post("/api/v1/catalogs", json=IMAGINARY)

    await _apply(applying_client)

    cluster = Trino(
        host=trino_cluster.get_container_host_ip(),
        port=int(trino_cluster.get_exposed_port(8080)),
    )
    assert "imaginary" not in await cluster.catalogs()
    assert SECRET not in fake_kubernetes.secrets


async def test_the_failure_names_the_resource_and_the_reason(
    applying_client: AsyncClient,
) -> None:
    await applying_client.post("/api/v1/catalogs", json=IMAGINARY)

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "failed"
    assert [(f["section"], f["resource"]) for f in verdict["failures"]] == [
        ("catalogs", "imaginary")
    ]
    assert "nosuchconnector" in verdict["failures"][0]["reason"]


async def test_validation_runs_once_per_apply_not_once_per_catalog(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """A pod per resource would be slow and expensive for something the Validate
    action already offers on demand."""
    for name in ("one", "two", "three"):
        await applying_client.post("/api/v1/catalogs", json={**MEMORY, "name": name})

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert len(fake_kubernetes.pod_history) == 1


async def test_the_validation_pod_is_deleted_when_validation_passes(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/catalogs", json=MEMORY)

    await _apply(applying_client)

    assert fake_kubernetes.pod_history
    assert fake_kubernetes.pods == {}


async def test_the_validation_pod_is_deleted_when_validation_fails(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Cleanup on the failure path matters more than on the happy one: the failure
    path is the one that runs unattended at three in the morning."""
    await applying_client.post("/api/v1/catalogs", json=IMAGINARY)

    await _apply(applying_client)

    assert fake_kubernetes.pod_history
    assert fake_kubernetes.pods == {}


async def test_a_candidate_with_no_catalogs_needs_no_pod(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The pod is the expensive part. An empty Candidate is a real case: the first
    Apply of a fresh Cluster."""
    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert fake_kubernetes.pod_history == []


async def test_a_coordinator_that_never_serves_fails_validation(
    settings: Settings, fake_kubernetes: FakeKubernetes, trino_cluster: DockerContainer
) -> None:
    """The hard timeout. A pod that never becomes ready must fail Validation rather
    than hang the pipeline."""
    fake_kubernetes.pod_never_serves = True
    quick = settings.model_copy(update={"validation_timeout_seconds": 1.0})

    async with _running(quick, fake_kubernetes, trino_cluster) as client:
        await client.post("/api/v1/catalogs", json=MEMORY)

        verdict = await _validation(client)

    assert verdict["outcome"] == "failed"
    assert "not serving within 1s" in verdict["failures"][0]["reason"]
    assert fake_kubernetes.pods == {}


async def test_a_pod_that_cannot_start_fails_without_waiting_for_the_timeout(
    settings: Settings, fake_kubernetes: FakeKubernetes, trino_cluster: DockerContainer
) -> None:
    fake_kubernetes.pod_problem = "ImagePullBackOff: manifest unknown"
    patient = settings.model_copy(update={"validation_timeout_seconds": 600.0})

    async with _running(patient, fake_kubernetes, trino_cluster) as client:
        await client.post("/api/v1/catalogs", json=MEMORY)

        verdict = await _validation(client)

    assert verdict["outcome"] == "failed"
    assert "ImagePullBackOff" in verdict["failures"][0]["reason"]


async def test_the_validate_action_does_not_apply(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, trino_cluster: DockerContainer
) -> None:
    """The whole point of the separate action: an Operator can test a Candidate
    without promoting it."""
    await applying_client.post("/api/v1/catalogs", json=MEMORY)

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "passed"
    assert verdict["failures"] == []
    cluster = Trino(
        host=trino_cluster.get_container_host_ip(),
        port=int(trino_cluster.get_exposed_port(8080)),
    )
    assert "scratch" not in await cluster.catalogs()
    assert SECRET not in fake_kubernetes.secrets
    assert (await applying_client.get("/api/v1/snapshots")).json() == []


async def test_validating_does_not_freeze_the_candidate(applying_client: AsyncClient) -> None:
    """A Validation changes nothing, so there is nothing for a concurrent edit to
    corrupt."""
    await applying_client.post("/api/v1/catalogs", json=MEMORY)
    await applying_client.post("/api/v1/validations")

    added = await applying_client.post("/api/v1/catalogs", json={**MEMORY, "name": "during"})

    assert added.status_code == 201


async def test_validations_are_listed_newest_first(applying_client: AsyncClient) -> None:
    first = await _validation(applying_client)
    second = await _validation(applying_client)

    listed = [record["id"] for record in (await applying_client.get("/api/v1/validations")).json()]

    assert listed[:2] == [second["id"], first["id"]]


async def test_an_unknown_validation_is_not_found(applying_client: AsyncClient) -> None:
    missing = await applying_client.get("/api/v1/validations/val_nope")

    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


async def test_a_catalog_named_after_an_example_catalog_still_validates(
    applying_client: AsyncClient,
) -> None:
    """The Trino image ships example catalogs -- jmx, memory, tpch, tpcds -- which the
    ephemeral coordinator must not hold. Otherwise a Catalog an Operator quite
    reasonably named `memory` fails Validation as already existing, for a reason
    having nothing to do with the Candidate."""
    await applying_client.post("/api/v1/catalogs", json={**MEMORY, "name": "memory"})

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "passed", verdict["failures"]


async def test_a_name_already_on_the_cluster_fails_validation(
    applying_client: AsyncClient,
) -> None:
    """The ephemeral pod cannot catch this one: it starts empty, so the collision
    exists only on the Cluster. Caught before Apply writes anything."""
    await applying_client.post("/api/v1/catalogs", json={**MEMORY, "name": "system"})

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "failed"
    assert verdict["failures"][0]["resource"] == "system"
    assert "already exists on the Cluster" in verdict["failures"][0]["reason"]


async def test_orphaned_validation_pods_are_swept_at_startup(
    settings: Settings, fake_kubernetes: FakeKubernetes, trino_cluster: DockerContainer
) -> None:
    """A crash mid-validation leaks a pod, and leaked Trino pods are not cheap."""
    await fake_kubernetes.create_pod(
        {"metadata": {"name": "apchi-validate-orphan", "labels": {ROLE_LABEL: VALIDATION_ROLE}}}
    )

    async with _running(settings, fake_kubernetes, trino_cluster):
        pass

    assert fake_kubernetes.pods == {}

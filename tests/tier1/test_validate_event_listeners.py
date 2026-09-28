"""Validating Event Listeners against the ephemeral coordinator.

The file-based Validation mode, used here for the first time. Catalogs are proved by
issuing statements against the probe; a Section whose configuration *is* a file is proved
by starting the probe with it in place, so a file Trino will not accept becomes a pod that
will not start rather than a coordinator that never comes back.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from httpx import ASGITransport, AsyncClient
from testcontainers.core.container import DockerContainer

from app.adapters.trino import Trino
from app.config import Settings
from app.main import create_app
from app.pipeline.applies import TERMINAL
from app.pipeline.validation import ROLE_LABEL, VALIDATION_ROLE, probe_secret
from app.sections.event_listeners.generator import FILE_KEY, MOUNT_PATH
from tests.conftest import FakeKubernetes

HTTP = {
    "name": "audit",
    "type": "http",
    "properties": {"http-event-listener.connect-ingest-uri": "http://collector:8080/events"},
}
CATALOG = {"name": "scratch", "connector": "memory", "properties": {}}


async def _validation(client: AsyncClient, timeout: float = 90.0) -> dict:
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


async def _apply(client: AsyncClient, timeout: float = 90.0) -> dict:
    started = await client.post("/api/v1/applies")
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError("Apply never settled")


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
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as client,
        app.router.lifespan_context(app),
    ):
        yield client


def _pod(kubernetes: FakeKubernetes) -> dict:
    return kubernetes.pod_manifests[kubernetes.pod_history[-1]]


async def test_the_probe_is_started_with_the_listener_file_in_place(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Starting is the check: a listener Trino will not accept is a pod that will not
    start."""
    await applying_client.post("/api/v1/event-listeners", json=HTTP)

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "passed", verdict["failures"]
    pod = _pod(fake_kubernetes)
    mounts = {
        m["mountPath"]: m.get("subPath") for m in pod["spec"]["containers"][0]["volumeMounts"]
    }
    assert mounts[MOUNT_PATH] == FILE_KEY


async def test_the_probe_file_carries_the_candidates_configuration(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/event-listeners", json=HTTP)

    await _validation(applying_client)

    delivered = fake_kubernetes.secret_history[fake_kubernetes.pod_history[-1]][FILE_KEY]
    assert "event-listener.name=http" in delivered
    assert "http-event-listener.connect-ingest-uri=http://collector:8080/events" in delivered


async def test_the_probes_secret_dies_with_it(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/event-listeners", json=HTTP)

    await _validation(applying_client)

    assert fake_kubernetes.pods == {}
    assert fake_kubernetes.pod_history[-1] not in fake_kubernetes.secrets


async def test_the_probes_secret_is_labelled_for_the_orphan_sweep(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """A crash mid-Validation must leave something the next start can find."""
    await applying_client.post("/api/v1/event-listeners", json=HTTP)
    await _validation(applying_client)

    assert probe_secret({MOUNT_PATH: "x"}) == {FILE_KEY: "x"}
    # The label the sweep selects on is the one the pod carries.
    assert _pod(fake_kubernetes)["metadata"]["labels"][ROLE_LABEL] == VALIDATION_ROLE


async def test_a_candidate_with_no_listener_mounts_nothing(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Catalogs are proved by statements, not by a file, so they contribute none."""
    await applying_client.post("/api/v1/catalogs", json=CATALOG)

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "passed", verdict["failures"]
    pod = _pod(fake_kubernetes)
    assert [m["mountPath"] for m in pod["spec"]["containers"][0]["volumeMounts"]] == [
        "/etc/trino/catalog"
    ]
    assert pod["spec"]["volumes"] == [{"name": "no-catalogs", "emptyDir": {}}]


async def test_an_empty_candidate_still_needs_no_pod(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The pod is the expensive part, and a listener does not change that."""
    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "passed"
    assert fake_kubernetes.pod_history == []


async def test_a_listener_needs_a_pod_even_with_no_catalogs(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/event-listeners", json=HTTP)

    await _validation(applying_client)

    assert len(fake_kubernetes.pod_history) == 1


async def test_a_probe_that_will_not_start_fails_validation_naming_the_listener(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """What an unreachable Kafka broker looks like: Trino refuses to start, and the reason
    exists only in the pod's log."""
    fake_kubernetes.pod_problem = "the pod failed"
    fake_kubernetes.pod_log = (
        "2026-09-24T00:00:00Z\tINFO\tmain\tio.trino.server.Server\tstarting\n"
        "2026-09-24T00:00:01Z\tERROR\tmain\tio.trino.server.Server\t"
        "Cannot connect to Kafka broker kafka-1:9093\n"
    )

    async with _running(settings, fake_kubernetes, trino_cluster, trino_validation) as client:
        await client.post("/api/v1/event-listeners", json=HTTP)

        verdict = await _validation(client)

    assert verdict["outcome"] == "failed"
    failure = verdict["failures"][0]
    assert failure["resource"] == "audit"
    assert "Cannot connect to Kafka broker" in failure["reason"]


async def test_a_listener_validation_failure_leaves_the_cluster_untouched(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """The point of the ephemeral pod: the coordinator is never restarted for a
    configuration it would have refused."""
    fake_kubernetes.pod_problem = "the pod failed"

    async with _running(settings, fake_kubernetes, trino_cluster, trino_validation) as client:
        await client.post("/api/v1/event-listeners", json=HTTP)

        record = await _apply(client)

    assert record["stage"] == "failed"
    assert fake_kubernetes.restarts == [], "nothing was applied, so nothing was restarted"
    assert fake_kubernetes.mounts == {}
    assert record["rollback"] is None


async def test_the_validate_action_covers_listeners_without_applying_them(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/event-listeners", json=HTTP)

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "passed", verdict["failures"]
    assert fake_kubernetes.restarts == []
    assert "trino-event-listener" not in fake_kubernetes.secrets
    assert (await applying_client.get("/api/v1/snapshots")).json() == []

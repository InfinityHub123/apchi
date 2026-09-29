"""Validating and applying the Certificate Mapping Pattern.

A rule list Trino will not parse is a pod that will not start, so Validation proves the
pattern against the ephemeral coordinator before the Cluster is touched. That only works if
the probe is configured to *read* the file, which the image is not: the probe's
config.properties is generated for exactly this reason.
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
from app.pipeline.impact import RESTART_WARNING
from app.sections.certificate_mapping.generator import FILE_KEY, MOUNT_PATH
from app.sections.event_listeners.generator import MOUNT_PATH as LISTENER_PATH
from tests.conftest import FakeKubernetes

PATTERN = {"pattern": "(.*)@example\\.com", "user": "$1", "case": "lower"}
HTTP = {
    "name": "audit",
    "type": "http",
    "properties": {"http-event-listener.connect-ingest-uri": "http://collector:8080/events"},
}


async def _validation(client: AsyncClient, timeout: float = 90.0) -> dict:
    started = await client.post("/api/v1/validations")
    assert started.status_code == 202
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/validations/{started.json()['id']}")).json()
        if record["outcome"] != "running":
            return record
        await asyncio.sleep(0.05)
    raise AssertionError("Validation never finished")


async def _apply(client: AsyncClient, timeout: float = 90.0) -> dict:
    started = await client.post("/api/v1/applies")
    deadline = asyncio.get_running_loop().time() + timeout
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


def _probe_files(kubernetes: FakeKubernetes) -> dict[str, str]:
    return kubernetes.secret_history[kubernetes.pod_history[-1]]


async def test_the_probe_is_told_to_read_the_mapping_file(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The image sets no user-mapping property, so a file mounted into a stock probe would
    be a file nothing reads -- and every pattern would pass."""
    await applying_client.put("/api/v1/certificate-mapping", json=PATTERN)

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "passed", verdict["failures"]
    mounts = {
        m["mountPath"]: m.get("subPath")
        for m in _pod(fake_kubernetes)["spec"]["containers"][0]["volumeMounts"]
    }
    assert mounts[MOUNT_PATH] == FILE_KEY
    config = _probe_files(fake_kubernetes)["config.properties"]
    assert f"http-server.authentication.insecure.user-mapping.file={MOUNT_PATH}" in config


async def test_the_probe_keeps_the_configuration_the_image_ships(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Replacing config.properties means owning everything that was in it."""
    await applying_client.put("/api/v1/certificate-mapping", json=PATTERN)

    await _validation(applying_client)

    config = _probe_files(fake_kubernetes)["config.properties"]
    assert "coordinator=true" in config
    assert "node-scheduler.include-coordinator=true" in config
    assert "catalog.management=${ENV:CATALOG_MANAGEMENT}" in config


async def test_the_probe_file_puts_apchis_own_rule_first(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """First match wins, so a pattern that would otherwise swallow Apchi's own principal
    cannot: the same protection §8 gives the verification identity."""
    await applying_client.put("/api/v1/certificate-mapping", json=PATTERN)

    await _validation(applying_client)

    delivered = _probe_files(fake_kubernetes)[FILE_KEY]
    assert delivered.index('"^apchi$"') < delivered.index('"(.*)@example')
    assert '"case": "lower"' in delivered


async def test_no_pattern_needs_no_probe(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The file Apchi writes for an empty Section is the behaviour Trino has anyway, so
    there is nothing for a coordinator to reject."""
    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "passed"
    assert fake_kubernetes.pod_history == []


async def test_a_pattern_trino_will_not_parse_fails_validation_before_the_cluster_is_touched(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """Java's regex engine is the authority, and it rejects things Python accepts. The
    reason exists only in the probe's log."""
    fake_kubernetes.pod_problem = "the pod failed"
    fake_kubernetes.pod_log = (
        "2026-09-29T00:00:00Z\tINFO\tmain\tio.trino.server.Server\tstarting\n"
        "2026-09-29T00:00:01Z\tERROR\tmain\tio.trino.server.Server\t"
        f"Invalid JSON file '{MOUNT_PATH}' for 'class UserMapping$UserMappingRules'\n"
    )

    async with _running(settings, fake_kubernetes, trino_cluster, trino_validation) as client:
        await client.put("/api/v1/certificate-mapping", json=PATTERN)

        verdict = await _validation(client)
        record = await _apply(client)

    failure = verdict["failures"][0]
    assert failure["resource"] == "pattern"
    assert "Invalid JSON file" in failure["reason"]
    assert record["stage"] == "failed"
    assert fake_kubernetes.restarts == [], "nothing was applied, so nothing was restarted"
    assert fake_kubernetes.mounts == {}


async def test_the_section_blamed_is_the_one_whose_file_trino_named(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """Two Sections now put files in front of the probe, so "the first one" is no longer
    an answer -- Trino names the file it choked on and that is what decides."""
    fake_kubernetes.pod_problem = "the pod failed"
    fake_kubernetes.pod_log = (
        "2026-09-29T00:00:01Z\tERROR\tmain\tio.trino.server.Server\t"
        f"Error loading event listener from {LISTENER_PATH}: unknown property\n"
    )

    async with _running(settings, fake_kubernetes, trino_cluster, trino_validation) as client:
        await client.put("/api/v1/certificate-mapping", json=PATTERN)
        await client.post("/api/v1/event-listeners", json=HTTP)

        verdict = await _validation(client)

    assert verdict["outcome"] == "failed"
    assert verdict["failures"][0]["resource"] == "audit"


async def test_applying_a_pattern_delivers_the_file_and_restarts_the_coordinator(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """UserMapping parses its rules when the authenticator is built, so the Rollout is the
    only way a pattern takes effect."""
    await applying_client.put("/api/v1/certificate-mapping", json=PATTERN)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    delivered = fake_kubernetes.secrets["trino-user-mapping"][FILE_KEY]
    assert '"(.*)@example\\\\.com"' in delivered
    assert fake_kubernetes.mounts["apchi-user-mapping"] == {
        "secret": "trino-user-mapping",
        "path": MOUNT_PATH,
        "key": FILE_KEY,
    }
    assert len(fake_kubernetes.restarts) == 1
    assert "certificate_mapping" in fake_kubernetes.restarts[0]


async def test_clearing_a_pattern_leaves_the_pass_through_rule_behind(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Unmounting is not an option here the way it is for a listener: the authenticator is
    configured to read the file, so "no pattern" has to be spelled out as a rule."""
    await applying_client.put("/api/v1/certificate-mapping", json=PATTERN)
    await _apply(applying_client)
    await applying_client.delete("/api/v1/certificate-mapping")

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    delivered = fake_kubernetes.secrets["trino-user-mapping"][FILE_KEY]
    assert '"@example' not in delivered
    assert '"pattern": "(.*)"' in delivered
    assert fake_kubernetes.mounts["apchi-user-mapping"] == {
        "secret": "trino-user-mapping",
        "path": MOUNT_PATH,
        "key": FILE_KEY,
    }


async def test_reverting_the_pattern_leaves_the_catalogs_alone_and_warns(
    applying_client: AsyncClient,
) -> None:
    """Recovery for this Section costs a restart, and a Section Revert must not be quieter
    about that than the change that made it necessary."""
    await applying_client.post(
        "/api/v1/catalogs", json={"name": "kept", "connector": "memory", "properties": {}}
    )
    await applying_client.put("/api/v1/certificate-mapping", json=PATTERN)
    await _apply(applying_client)
    await applying_client.put(
        "/api/v1/certificate-mapping", json={"pattern": "CN=(.*?),.*", "user": "$1"}
    )
    await _apply(applying_client)

    effect = (
        await applying_client.post("/api/v1/certificate-mapping/revert", json={"snapshot": 1})
    ).json()

    assert effect["sections"] == ["certificate_mapping"]
    assert effect["cost"]["restarts_coordinator"] is True
    assert RESTART_WARNING in effect["summary"]
    assert (await applying_client.get("/api/v1/certificate-mapping")).json()["pattern"] == (
        "(.*)@example\\.com"
    )
    assert [c["name"] for c in (await applying_client.get("/api/v1/catalogs")).json()] == ["kept"]

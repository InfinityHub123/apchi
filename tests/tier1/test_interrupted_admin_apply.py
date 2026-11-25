"""An Admin Apply interrupted by an Apchi restart is rolled back as an Admin Apply.

Recovery rolls back an interrupted Apply the way the Apply itself would have (§10). For an
Operator's that means keeping the Admin values -- they survive an Operator's rollback
(invariant 2). For an Admin Apply it means the opposite: its Admin values are what may have
broken the Cluster, so they go back to what the Cluster last had. Recovery used to roll every
interrupted Apply back with the Operator's engine, which left an interrupted Admin Apply's
properties and raw files in place -- on the Cluster and in the document.

A restart is simulated as in test_interrupted_apply.py: the runner's task is cancelled once
the Apply has delivered, then a fresh app over the same MongoDB runs recovery at startup.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from httpx import ASGITransport, AsyncClient
from testcontainers.core.container import DockerContainer

from app.adapters.trino import Trino
from app.config import Settings
from app.main import create_app
from app.pipeline.applies import ApplyRecord
from tests.conftest import SERVER_CONFIG_SECRET, FakeKubernetes, container_url

SERVER_CONFIGURATION = "/api/v1/admin/server-configuration"
GOOD = {"coordinator": {"config.properties": {"query.max-run-time": "4h"}}}
BAD = {"coordinator": {"config.properties": {"query.max-run-time": "1ms"}}}


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
    app.state.trino = Trino(url=container_url(cluster), user=settings.trino_user)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="https://apchi") as client,
        app.router.lifespan_context(app),
    ):
        client.app_under_test = app  # type: ignore[attr-defined]
        yield client


async def _settled(client: AsyncClient, apply_id: str) -> dict:
    deadline = asyncio.get_running_loop().time() + 120
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in ("succeeded", "failed", "incident"):
            return dict(record)
        await asyncio.sleep(0.05)
    raise AssertionError("the Apply never settled")


async def _interrupt_once_delivered(
    client: AsyncClient, kubernetes: FakeKubernetes, path: str, marker: str
) -> str:
    """Start an Apply and cancel it once the Cluster holds what it delivers."""
    started = await client.post(path, json={})
    apply_id = started.json()["id"]
    deadline = asyncio.get_running_loop().time() + 180
    while asyncio.get_running_loop().time() < deadline:
        if marker in kubernetes.secrets[SERVER_CONFIG_SECRET]["config.properties"]:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("the Apply never delivered")
    state = client.app_under_test.state  # type: ignore[attr-defined]
    runner = state.admin_apply_runner if "admin" in path else state.apply_runner
    await runner.shutdown()
    return apply_id


async def test_an_interrupted_admin_apply_is_put_back_to_what_the_cluster_had(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as first:
        await first.put(SERVER_CONFIGURATION, json=GOOD)
        good = await _settled(
            first, (await first.post("/api/v1/admin/applies", json={})).json()["id"]
        )
        assert good["stage"] == "succeeded", good
        delivered = dict(fake_kubernetes.secrets[SERVER_CONFIG_SECRET])
        await first.put(SERVER_CONFIGURATION, json=BAD)
        apply_id = await _interrupt_once_delivered(
            first, fake_kubernetes, "/api/v1/admin/applies", "query.max-run-time=1ms"
        )
    restarts_before = len(fake_kubernetes.restarts)

    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as restarted:
        record = (await restarted.get(f"/api/v1/applies/{apply_id}")).json()
        stored = (await restarted.get(SERVER_CONFIGURATION)).json()

    assert record["kind"] == "admin_apply"
    assert record["interrupted"] is True
    assert record["rollback"] == "succeeded", record
    assert stored["coordinator"]["config.properties"] == {"query.max-run-time": "4h"}
    assert fake_kubernetes.secrets[SERVER_CONFIG_SECRET] == delivered
    assert len(fake_kubernetes.restarts) > restarts_before, (
        "the coordinator was never restarted onto the restored files"
    )


async def test_an_interrupted_operator_apply_still_keeps_the_admin_values(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    """Invariant 2, unchanged: an Operator's rollback is not the Admin's to undo."""
    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as first:
        await first.put(SERVER_CONFIGURATION, json=GOOD)
        apply_id = await _interrupt_once_delivered(
            first, fake_kubernetes, "/api/v1/applies", "query.max-run-time=4h"
        )

    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as restarted:
        record = (await restarted.get(f"/api/v1/applies/{apply_id}")).json()
        stored = (await restarted.get(SERVER_CONFIGURATION)).json()

    assert record["kind"] == "apply"
    assert record["interrupted"] is True
    assert stored["coordinator"]["config.properties"] == {"query.max-run-time": "4h"}


async def test_each_record_says_which_pipeline_ran(
    settings: Settings,
    fake_kubernetes: FakeKubernetes,
    trino_cluster: DockerContainer,
    trino_validation: DockerContainer,
) -> None:
    async with _apchi(settings, fake_kubernetes, trino_cluster, trino_validation) as client:
        operator = await _settled(client, (await client.post("/api/v1/applies")).json()["id"])
        admin = await _settled(
            client, (await client.post("/api/v1/admin/applies", json={})).json()["id"]
        )

    assert operator["kind"] == "apply"
    assert admin["kind"] == "admin_apply"


def test_a_record_from_before_the_field_reads_as_an_operators() -> None:
    """Recorded before Apchi kept the kind, so recovered as it always was."""
    record = ApplyRecord.model_validate(
        {"id": "apl_old", "stage": "applying", "started_at": "2026-01-01T00:00:00Z"}
    )

    assert record.kind == "apply"

"""The rollout Apply engine against a real cluster.

Only a real Kubernetes and a real Trino can prove the thing slice 2 exists to prove: that
the pod template patch Apchi makes produces a coordinator which comes back, and that the
listener file it delivered is actually loaded. Adoption is read from the coordinator's log,
because nothing functional can tell Apchi whether an event listener loaded.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.adapters.kubernetes import RealKubernetes
from app.adapters.trino import Trino
from app.pipeline.applies import TERMINAL
from app.sections.event_listeners.generator import FILE_KEY
from tests.tier2.conftest import (
    EVENT_LISTENER_SECRET,
    ForwardedKubernetes,
    PortForward,
    coordinator_log,
)

pytestmark = pytest.mark.tier2

HTTP = {
    "name": "audit",
    "type": "http",
    "properties": {
        "http-event-listener.connect-ingest-uri": "http://collector.invalid:8080/events",
        "http-event-listener.log-completed": "true",
    },
}
CATALOG = {"name": "kept", "connector": "memory", "properties": {}}


async def _apply(client: AsyncClient, timeout: float = 900.0) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    apply_id = started.json()["id"]
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(1)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


async def test_an_apply_rolls_out_the_coordinator_and_trino_loads_the_listener(
    e2e_client: AsyncClient,
    forward: PortForward,
    real_kubernetes: RealKubernetes,
    forwarded_kubernetes: ForwardedKubernetes,
) -> None:
    """The tracer bullet for the engine. The coordinator is replaced, comes back, and is
    running the configuration Apchi delivered."""
    await e2e_client.post("/api/v1/event-listeners", json=HTTP)

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert "rolling_out" in [event["stage"] for event in record["history"]]
    assert record["snapshot"] == 1

    delivered = await real_kubernetes.read_secret(EVENT_LISTENER_SECRET)
    assert "event-listener.name=http" in delivered[FILE_KEY]

    # Apchi owns exactly one volume on the pod template, and only while a listener exists.
    spec = await real_kubernetes.deployment_pod_spec("trino-coordinator")
    assert "apchi-event-listener" in [volume["name"] for volume in spec["volumes"]]

    # Adoption, read from the coordinator itself.
    forward.restart()
    log = coordinator_log()
    assert "http-event-listener" in log


async def test_the_coordinator_serves_queries_after_the_rollout(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """Verification already asserts this, but only by a query Apchi itself ran. This checks
    the Cluster an End User would find."""
    await e2e_client.post("/api/v1/catalogs", json=CATALOG)
    await e2e_client.post("/api/v1/event-listeners", json=HTTP)

    record = await _apply(e2e_client)
    assert record["stage"] == "succeeded", record.get("failure_reason")

    forward.restart()
    trino = Trino(host="127.0.0.1", port=forward.port)

    assert "kept" in await trino.catalogs()
    assert await trino.query("SELECT 1 FROM system.runtime.nodes LIMIT 1")


async def test_removing_the_listener_unmounts_it_and_the_coordinator_still_starts(
    e2e_client: AsyncClient, forward: PortForward, real_kubernetes: RealKubernetes
) -> None:
    """The case that decided the delivery mechanism. A Secret with no key becomes a
    directory under a subPath mount and Trino dies on it, so removing the last listener has
    to remove the mount -- and the coordinator has to come back without it."""
    await e2e_client.post("/api/v1/event-listeners", json=HTTP)
    await _apply(e2e_client)

    await e2e_client.delete("/api/v1/event-listeners/audit")
    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    spec = await real_kubernetes.deployment_pod_spec("trino-coordinator")
    assert "apchi-event-listener" not in [volume["name"] for volume in spec["volumes"]]
    assert "/etc/trino/event-listener.properties" not in [
        mount["mountPath"] for mount in spec["containers"][0]["volumeMounts"]
    ]

    forward.restart()
    assert await Trino(host="127.0.0.1", port=forward.port).is_starting() is False


async def test_a_catalogs_only_apply_does_not_restart_the_coordinator(
    e2e_client: AsyncClient, real_kubernetes: RealKubernetes
) -> None:
    """Half the Sections reach the Cluster without a restart. Proven here by the pod
    surviving: routine catalog work must not cost anybody their queries."""
    before = await real_kubernetes.rollout_state("trino-coordinator")

    await e2e_client.post("/api/v1/catalogs", json=CATALOG)
    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert "rolling_out" not in [event["stage"] for event in record["history"]]
    after = await real_kubernetes.rollout_state("trino-coordinator")
    assert after.generation == before.generation, "the pod template was not touched"

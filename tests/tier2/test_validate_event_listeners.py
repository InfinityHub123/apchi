"""Validating the Admin's Event Listener against a real ephemeral coordinator (#269).

Tier 1 proves Apchi puts the listener in front of the probe. Only a real Trino proves that a
listener it will not load becomes a probe that will not start, and that an Admin Apply turns
that into a refusal rather than a coordinator that never comes back.

The Kafka listener is the instrument. `terminate-on-initialization-failure` defaults to true,
so brokers that cannot be reached make Trino refuse to start -- which needs no Kafka and no
extra infrastructure to arrange.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.adapters.kubernetes import RealKubernetes
from app.pipeline.applies import TERMINAL
from app.pipeline.validation import VALIDATION_SELECTOR
from tests.tier2.conftest import ForwardedKubernetes

pytestmark = pytest.mark.tier2

SERVER_CONFIGURATION = "/api/v1/admin/server-configuration"
UNREACHABLE_KAFKA = {
    "event-listener.name": "kafka",
    "kafka-event-listener.broker-endpoints": "nowhere.invalid:9093",
    "kafka-event-listener.created-event.topic": "created",
    "kafka-event-listener.completed-event.topic": "completed",
    "kafka-event-listener.client-id": "apchi-tier2",
}
HTTP = {
    "event-listener.name": "http",
    "http-event-listener.connect-ingest-uri": "http://collector.invalid:8080/e",
}


async def _admin_apply(client: AsyncClient, listener: dict[str, str]) -> dict:
    written = await client.put(
        SERVER_CONFIGURATION, json={"coordinator": {"event-listener.properties": listener}}
    )
    assert written.status_code == 200, written.text
    started = await client.post("/api/v1/admin/applies", json={})
    assert started.status_code == 202, started.text
    deadline = asyncio.get_running_loop().time() + 900
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(1)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


async def test_a_listener_a_real_trino_accepts_is_probed_applied_and_cleaned_up(
    e2e_client: AsyncClient,
    forwarded_kubernetes: ForwardedKubernetes,
    real_kubernetes: RealKubernetes,
) -> None:
    """The probe starts with the listener file in place, and starting is the check. A probe
    brings a Secret with it, and an orphan of either kind is a leak. One Apply for both,
    because it restarts the coordinator and each test pays a restart to be put back (#300)."""
    record = await _admin_apply(e2e_client, HTTP)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert forwarded_kubernetes.created_pods, "the listener was never probed"
    leftover = await real_kubernetes.delete_secrets(VALIDATION_SELECTOR)
    assert leftover == [], f"validation Secrets left behind: {leftover}"


async def test_a_kafka_listener_with_unreachable_brokers_never_reaches_the_cluster(
    e2e_client: AsyncClient, real_kubernetes: RealKubernetes
) -> None:
    """The failure this exists to catch. Without the probe the Apply would restart the
    coordinator, and the coordinator would not come back."""
    before = await real_kubernetes.rollout_state("trino-coordinator")

    record = await _admin_apply(e2e_client, UNREACHABLE_KAFKA)

    assert record["stage"] == "failed", record
    assert record["failure_reason"], "the reason exists only in the probe's log"
    assert record["rollback"] is None, "nothing was touched, so nothing is owed"
    after = await real_kubernetes.rollout_state("trino-coordinator")
    assert after.generation == before.generation, "the pod template was never patched"

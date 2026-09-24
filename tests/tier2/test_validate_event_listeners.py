"""Validating Event Listeners against a real ephemeral coordinator.

Tier 1 proves Apchi asks for the right pod. Only a real Trino proves the asking works: that
a listener configuration it will not accept becomes a pod that will not start, and that
Apchi turns that into a Validation failure rather than a coordinator that never comes back.

The Kafka listener is the instrument. `terminate-on-initialization-failure` defaults to
true, so brokers that cannot be reached make Trino refuse to start -- which needs no Kafka
and no extra infrastructure to arrange.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.adapters.kubernetes import RealKubernetes
from app.pipeline.applies import TERMINAL
from app.pipeline.validation import VALIDATION_SELECTOR
from tests.tier2.conftest import ForwardedKubernetes

pytestmark = pytest.mark.tier2

UNREACHABLE_KAFKA = {
    "name": "pipeline",
    "type": "kafka",
    "properties": {
        "kafka-event-listener.broker-endpoints": "nowhere.invalid:9093",
        "kafka-event-listener.created-event.topic": "created",
        "kafka-event-listener.completed-event.topic": "completed",
        "kafka-event-listener.client-id": "apchi-tier2",
    },
}
HTTP = {
    "name": "audit",
    "type": "http",
    "properties": {"http-event-listener.connect-ingest-uri": "http://collector.invalid:8080/e"},
}


async def _validation(client: AsyncClient, timeout: float = 600.0) -> dict:
    started = await client.post("/api/v1/validations")
    assert started.status_code == 202
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/validations/{started.json()['id']}")).json()
        if record["outcome"] != "running":
            return record
        await asyncio.sleep(1)
    raise AssertionError("Validation never finished")


async def test_a_listener_a_real_trino_accepts_passes_validation(
    e2e_client: AsyncClient, forwarded_kubernetes: ForwardedKubernetes
) -> None:
    """The probe starts with the listener file in place, and starting is the check."""
    await e2e_client.post("/api/v1/event-listeners", json=HTTP)

    verdict = await _validation(e2e_client)

    assert verdict["outcome"] == "passed", verdict["failures"]
    assert len(forwarded_kubernetes.created_pods) == 1


async def test_a_kafka_listener_with_unreachable_brokers_fails_validation(
    e2e_client: AsyncClient,
) -> None:
    """The failure this whole mode exists to catch. Without it the Apply would restart the
    coordinator, and the coordinator would not come back."""
    await e2e_client.post("/api/v1/event-listeners", json=UNREACHABLE_KAFKA)

    verdict = await _validation(e2e_client)

    assert verdict["outcome"] == "failed"
    failure = verdict["failures"][0]
    assert failure["resource"] == "pipeline", "the Operator is told which listener"
    assert failure["reason"], "and given the reason, which exists only in the pod's log"


async def test_an_apply_with_a_rejected_listener_never_touches_the_cluster(
    e2e_client: AsyncClient, real_kubernetes: RealKubernetes
) -> None:
    """Validation runs before anything is touched, so the coordinator is never restarted for
    a configuration Trino would have refused."""
    before = await real_kubernetes.rollout_state("trino-coordinator")
    await e2e_client.post("/api/v1/event-listeners", json=UNREACHABLE_KAFKA)

    started = await e2e_client.post("/api/v1/applies")
    deadline = asyncio.get_running_loop().time() + 600
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await e2e_client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            break
        await asyncio.sleep(1)

    assert record["stage"] == "failed"
    assert record["rollback"] is None, "nothing was touched, so nothing is owed"
    after = await real_kubernetes.rollout_state("trino-coordinator")
    assert after.generation == before.generation, "the pod template was never patched"


async def test_the_probe_and_its_secret_are_both_gone_afterwards(
    e2e_client: AsyncClient, real_kubernetes: RealKubernetes
) -> None:
    """A probe now brings a Secret with it, and an orphan of either kind is a leak."""
    await e2e_client.post("/api/v1/event-listeners", json=HTTP)

    await _validation(e2e_client)

    leftover = await real_kubernetes.delete_secrets(VALIDATION_SELECTOR)
    assert leftover == [], f"validation Secrets left behind: {leftover}"

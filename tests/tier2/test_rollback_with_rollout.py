"""Auto Rollback of a rollout-required change, against a real cluster.

The failure is forced with a deliberately small rollout timeout rather than by breaking the
coordinator's image or manifest -- that would be testing Kubernetes. A timeout is also the
one failure mode the hard bound exists for, so forcing it exercises the thing rather than
simulating it.

Note what that costs the test: the rollback's own rollout is bounded by the same small
timeout, so it exceeds it too and the outcome is an incident. What is provable here is that
the Cluster's *configuration* is put back -- the durable half -- and that Apchi then stops
touching it. The rollback that succeeds end to end is tier 1's, where Verification can be
made to fail exactly once.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.adapters.kubernetes import RealKubernetes
from app.adapters.trino import Trino
from app.config import Settings
from app.pipeline.applies import TERMINAL
from app.pipeline.auto_rollback import INCIDENT_MESSAGE
from app.sections.event_listeners.generator import FILE_KEY
from tests.tier2.conftest import (
    EVENT_LISTENER_SECRET,
    ForwardedKubernetes,
    PortForward,
    running_apchi,
)

pytestmark = pytest.mark.tier2

HTTP = {
    "name": "audit",
    "type": "http",
    "properties": {"http-event-listener.connect-ingest-uri": "http://collector.invalid:8080/e"},
}


async def _settled(client: AsyncClient, apply_id: str, timeout: float = 900.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(1)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


async def test_a_rollout_that_exceeds_its_timeout_puts_the_configuration_back(
    e2e_client: AsyncClient,
    settings: Settings,
    forwarded_kubernetes: ForwardedKubernetes,
    forward: PortForward,
    real_kubernetes: RealKubernetes,
) -> None:
    impatient = settings.model_copy(update={"rollout_timeout_seconds": 1.0})

    async with running_apchi(impatient, forwarded_kubernetes, forward) as client:
        await client.post("/api/v1/event-listeners", json=HTTP)
        started = await client.post("/api/v1/applies")
        record = await _settled(client, started.json()["id"])

        assert record["stage"] == "incident", record.get("failure_reason")
        assert record["rollback"] == "failed"
        assert record["operator_message"] == INCIDENT_MESSAGE
        assert "did not finish rolling out" in record["failure_reason"]

        state = (await client.get("/api/v1/admin/maintenance-mode")).json()
        assert state["engaged"] is True
        assert (
            await client.post("/api/v1/catalogs", json={"name": "blocked", "connector": "memory"})
        ).status_code == 409

    # The durable half is back where the Snapshot left it: no listener was ever committed,
    # so the rollback removed the one the failed Apply delivered.
    assert FILE_KEY not in await real_kubernetes.read_secret(EVENT_LISTENER_SECRET)
    spec = await real_kubernetes.deployment_pod_spec("trino-coordinator")
    assert "apchi-event-listener" not in [volume["name"] for volume in spec["volumes"]]

    assert (await e2e_client.get("/api/v1/snapshots")).json() == [], "no Snapshot was created"


async def test_the_coordinator_comes_back_on_the_restored_configuration(
    e2e_client: AsyncClient,
    settings: Settings,
    forwarded_kubernetes: ForwardedKubernetes,
    forward: PortForward,
) -> None:
    """The timeout was Apchi giving up, not the Cluster failing. Once the rollouts settle,
    the coordinator is serving what the Snapshot said -- which here is no listener at all."""
    impatient = settings.model_copy(update={"rollout_timeout_seconds": 1.0})

    async with running_apchi(impatient, forwarded_kubernetes, forward) as client:
        await client.post("/api/v1/event-listeners", json=HTTP)
        started = await client.post("/api/v1/applies")
        await _settled(client, started.json()["id"])

    from tests.tier2.conftest import kubectl

    kubectl("rollout", "status", "deploy/trino-coordinator", "--timeout=600s")
    forward.restart()

    assert await Trino(host="127.0.0.1", port=forward.port).is_starting() is False

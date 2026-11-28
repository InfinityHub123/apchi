"""The rollout Apply engine against a real cluster, with the Admin's Event Listener.

Only a real Kubernetes and a real Trino can prove what this exists to prove: that a restart
Apchi makes produces a coordinator which comes back, and that the listener it delivered is
actually loaded. Adoption is read from the coordinator's log, because nothing functional can
tell Apchi whether an event listener loaded.

The listener is the Admin's since #269: a file in the coordinator's Server Configuration
Secret, which the chart mounts whole at /etc/trino/apchi, named by `event-listener.config-files`
only when there is one. So what is proved here too is that Apchi never touches the
Deployment's volumes for it -- the restart is the only change to the pod template.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.adapters.kubernetes import RealKubernetes
from app.adapters.trino import Trino
from app.pipeline.applies import TERMINAL
from tests.tier2.conftest import (
    SERVER_CONFIG_SECRET,
    PortForward,
    coordinator_log,
    pod_template,
)

pytestmark = pytest.mark.tier2

SERVER_CONFIGURATION = "/api/v1/admin/server-configuration"
HTTP = {
    "event-listener.name": "http",
    "http-event-listener.connect-ingest-uri": "http://collector.invalid:8080/events",
    "http-event-listener.log-completed": "true",
}
LOADED = "-- Loaded event listener /etc/trino/apchi/event-listener.properties --"
CATALOG = {"name": "kept", "connector": "memory", "properties": {}}


async def _settled(client: AsyncClient, apply_id: str, timeout: float = 900.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(1)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


async def _apply(client: AsyncClient) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    return await _settled(client, started.json()["id"])


async def _admin_apply(client: AsyncClient, listener: dict[str, str] | None) -> dict:
    coordinator = {"event-listener.properties": listener} if listener else {}
    written = await client.put(SERVER_CONFIGURATION, json={"coordinator": coordinator})
    assert written.status_code == 200, written.text
    started = await client.post("/api/v1/admin/applies", json={})
    assert started.status_code == 202, started.text
    return await _settled(client, started.json()["id"])


def _volumes_and_mounts(spec: dict) -> tuple[list[str], list[str]]:
    container = next(c for c in spec["containers"] if c["name"] == "trino")
    return (
        sorted(volume["name"] for volume in spec["volumes"]),
        sorted(mount["mountPath"] for mount in container["volumeMounts"]),
    )


async def test_a_listener_rolls_out_and_back_and_a_catalog_never_restarts_anything(
    e2e_client: AsyncClient, forward: PortForward, real_kubernetes: RealKubernetes
) -> None:
    """One coordinator through a catalog, a listener and its removal. One scenario, because
    each rollout here is the expensive part and each test paid its own and a reset (#300).

    - **A catalogs-only Apply does not restart the coordinator.** Half the Sections reach the
      Cluster without a restart; routine catalog work must not cost anybody their queries.
    - **An Admin Apply restarts the coordinator and Trino loads the listener** -- read from
      where the chart mounts it, with nothing mounted for it by Apchi.
    - **The coordinator serves queries after the rollout**, as an End User would find it,
      not only through the query Verification ran.
    - **Removing the listener brings the coordinator back without it.** The case that
      decided the old delivery: an empty Secret key under a subPath mount is a directory
      Trino dies on. Here no mount names the key, and the pointer goes with it.
    """
    template = pod_template()
    await e2e_client.post("/api/v1/catalogs", json=CATALOG)
    record = await _apply(e2e_client)
    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert "rolling_out" not in [event["stage"] for event in record["history"]]
    assert pod_template() == template, "the pod template was not touched"

    before = _volumes_and_mounts(await real_kubernetes.deployment_pod_spec("trino-coordinator"))
    record = await _admin_apply(e2e_client, HTTP)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert "rolling_out" in [event["stage"] for event in record["history"]]
    delivered = await real_kubernetes.read_secret(SERVER_CONFIG_SECRET)
    assert delivered["event-listener.properties"].startswith("event-listener.name=http\n")
    assert (
        "event-listener.config-files=/etc/trino/apchi/event-listener.properties"
        in delivered["config.properties"]
    )
    after = _volumes_and_mounts(await real_kubernetes.deployment_pod_spec("trino-coordinator"))
    assert after == before, "Apchi changed the coordinator's volumes for a listener"
    forward.restart()
    assert LOADED in coordinator_log(2000)
    trino = Trino(url=f"http://127.0.0.1:{forward.port}")
    assert "kept" in await trino.catalogs()
    assert await trino.query("SELECT 1 FROM system.runtime.nodes LIMIT 1")

    record = await _admin_apply(e2e_client, None)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    delivered = await real_kubernetes.read_secret(SERVER_CONFIG_SECRET)
    assert "event-listener.properties" not in delivered
    assert "event-listener.config-files" not in delivered["config.properties"]
    forward.restart()
    assert await Trino(url=f"http://127.0.0.1:{forward.port}").is_starting() is False
    assert LOADED not in coordinator_log(2000)

"""Section Revert against a real cluster.

A revert is not a paper change: applying it issues real DROP CATALOG statements and
rewrites the durable copy. This proves both, and that the drop survives the pod --
removing a catalog from Trino while leaving it in the Secret would bring it back at
the next restart.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.adapters.kubernetes import RealKubernetes
from app.adapters.trino import Trino
from app.pipeline.applies import TERMINAL
from tests.tier2.conftest import CATALOG_SEED_SECRET, PortForward, restart_coordinator

pytestmark = pytest.mark.tier2

ALPHA = {"name": "alpha", "connector": "memory", "properties": {}}
BETA = {"name": "beta", "connector": "memory", "properties": {}}


async def _apply(client: AsyncClient, timeout: float = 600.0) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    apply_id = started.json()["id"]
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            assert record["stage"] == "succeeded", record.get("failure_reason")
            return record
        await asyncio.sleep(0.5)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


async def test_a_revert_drops_a_catalog_for_real_and_the_drop_survives_a_restart(
    e2e_client: AsyncClient, forward: PortForward, real_kubernetes: RealKubernetes
) -> None:
    await e2e_client.post("/api/v1/catalogs", json=ALPHA)
    await _apply(e2e_client)
    await e2e_client.post("/api/v1/catalogs", json=BETA)
    await _apply(e2e_client)

    effect = (await e2e_client.post("/api/v1/catalogs/revert", json={"snapshot": 1})).json()
    assert effect["catalogs_dropped"] == ["beta"]
    record = await _apply(e2e_client)

    assert record["snapshot"] == 3
    live = await Trino(host="127.0.0.1", port=forward.port).catalogs()
    assert "alpha" in live
    assert "beta" not in live
    assert set(await real_kubernetes.read_secret(CATALOG_SEED_SECRET)) == {"alpha.properties"}

    restart_coordinator()
    forward.restart()

    after_restart = await Trino(host="127.0.0.1", port=forward.port).catalogs()
    assert "alpha" in after_restart
    assert "beta" not in after_restart


async def test_reverting_a_listener_restarts_the_coordinator_and_takes_effect(
    e2e_client: AsyncClient, forward: PortForward, real_kubernetes: RealKubernetes
) -> None:
    """A recovery action for a rollout-required Section is not a paper change: it restarts
    the Cluster, and the effect says so before an Operator asks for it."""
    from app.sections.event_listeners.generator import FILE_KEY
    from tests.tier2.conftest import EVENT_LISTENER_SECRET

    listener = {
        "name": "audit",
        "type": "http",
        "properties": {"http-event-listener.connect-ingest-uri": "http://first.invalid:8080/e"},
    }
    await e2e_client.post("/api/v1/event-listeners", json=listener)
    await _apply(e2e_client)

    await e2e_client.patch(
        "/api/v1/event-listeners/audit",
        json={
            "properties": {"http-event-listener.connect-ingest-uri": "http://second.invalid:9/e"}
        },
    )
    await _apply(e2e_client)

    effect = (await e2e_client.post("/api/v1/event-listeners/revert", json={"snapshot": 1})).json()
    assert effect["cost"]["restarts_coordinator"] is True

    record = await _apply(e2e_client)

    assert record["snapshot"] == 3
    assert record["rolled_out"] is True
    delivered = await real_kubernetes.read_secret(EVENT_LISTENER_SECRET)
    assert "http://first.invalid:8080/e" in delivered[FILE_KEY]
    original = (await e2e_client.get("/api/v1/snapshots/1")).json()
    assert (await e2e_client.get("/api/v1/snapshots/3")).json()["sections"][
        "event_listeners"
    ] == original["sections"]["event_listeners"]


async def test_a_full_rollback_restores_a_snapshot_without_modifying_it(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """Snapshot 1 becomes Snapshot 3's content. Snapshot 1 itself is untouched."""
    await e2e_client.post("/api/v1/catalogs", json=ALPHA)
    await _apply(e2e_client)
    await e2e_client.post("/api/v1/catalogs", json=BETA)
    await _apply(e2e_client)

    await e2e_client.post("/api/v1/candidate/rollback", json={"snapshot": 1})
    await _apply(e2e_client)

    original = (await e2e_client.get("/api/v1/snapshots/1")).json()
    restored = (await e2e_client.get("/api/v1/snapshots/3")).json()
    assert restored["sections"] == original["sections"]
    assert original["apply_id"] != restored["apply_id"]
    assert "beta" not in await Trino(host="127.0.0.1", port=forward.port).catalogs()

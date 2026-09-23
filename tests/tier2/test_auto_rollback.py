"""Auto Rollback against a real cluster.

The failure here is genuine and comes from the one thing the ephemeral validation
coordinator cannot know about: the Cluster's access control. Catalog DDL is restricted
to Apchi's own Trino identity, so an Apchi configured with any other identity is
refused by the Cluster after passing Validation -- a real mid-Apply failure, at the
point Apply has already written the durable copy of the configuration.

That is section 10's first divergence: the Secret says one thing and Trino says
another. Restoring it is what stops the catalog reappearing at the next pod restart,
which the restart test here proves.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.adapters.kubernetes import RealKubernetes
from app.config import Settings
from app.pipeline.applies import TERMINAL
from app.pipeline.auto_rollback import INCIDENT_MESSAGE
from tests.tier2.conftest import (
    CATALOG_SEED_SECRET,
    ForwardedKubernetes,
    PortForward,
    restart_coordinator,
    running_apchi,
)

pytestmark = pytest.mark.tier2

KEPT = {"name": "kept", "connector": "memory", "properties": {}}
ADDED = {"name": "added", "connector": "memory", "properties": {}}


async def _settled(client: AsyncClient, apply_id: str, timeout: float = 600.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.5)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


async def _apply(client: AsyncClient) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    return await _settled(client, started.json()["id"])


async def _snapshot_one(client: AsyncClient) -> None:
    await client.post("/api/v1/catalogs", json=KEPT)
    record = await _apply(client)
    assert record["stage"] == "succeeded", record.get("failure_reason")


async def test_a_real_mid_apply_failure_returns_the_cluster_to_its_latest_snapshot(
    e2e_client: AsyncClient,
    settings: Settings,
    forwarded_kubernetes: ForwardedKubernetes,
    forward: PortForward,
    real_kubernetes: RealKubernetes,
) -> None:
    await _snapshot_one(e2e_client)

    # Same cluster, same MongoDB, an identity the Cluster will not let create catalogs.
    refused = settings.model_copy(update={"trino_user": "notapchi"})
    async with running_apchi(refused, forwarded_kubernetes, forward) as client:
        await client.post("/api/v1/catalogs", json=ADDED)

        record = await _apply(client)

    assert record["stage"] == "failed"
    assert record["rollback"] == "succeeded"
    assert "Access Denied" in record["failure_reason"]
    # The durable copy is back to the Snapshot: the failed Apply had already written it.
    assert set(await real_kubernetes.read_secret(CATALOG_SEED_SECRET)) == {"kept.properties"}
    assert (await e2e_client.get("/api/v1/snapshots")).json()[0]["number"] == 1
    assert len((await e2e_client.get("/api/v1/snapshots")).json()) == 1


async def test_what_the_rollback_restored_survives_a_coordinator_restart(
    e2e_client: AsyncClient,
    settings: Settings,
    forwarded_kubernetes: ForwardedKubernetes,
    forward: PortForward,
) -> None:
    """Why restoring the Secret is half the job. Had Auto Rollback left the failed
    Apply's Secret in place, the catalog it never managed to create would arrive at the
    next pod restart -- weeks later, with nothing linking it to this Apply."""
    await _snapshot_one(e2e_client)
    refused = settings.model_copy(update={"trino_user": "notapchi"})
    async with running_apchi(refused, forwarded_kubernetes, forward) as client:
        await client.post("/api/v1/catalogs", json=ADDED)
        record = await _apply(client)
        assert record["rollback"] == "succeeded"

    restart_coordinator()
    forward.restart()

    live = await _live_catalogs(forward)
    assert "kept" in live
    assert "added" not in live


async def test_a_failed_rollback_ends_in_an_incident_and_engages_maintenance_mode(
    e2e_client: AsyncClient,
    settings: Settings,
    forwarded_kubernetes: ForwardedKubernetes,
    forward: PortForward,
) -> None:
    """A Cluster that cannot verify at all: the Apply fails, and so does the one attempt
    to put it back. Apchi stops touching it."""
    broken = settings.model_copy(
        update={"trino_user": "notapchi", "verification_catalog": "no_such_catalog"}
    )
    async with running_apchi(broken, forwarded_kubernetes, forward) as client:
        await client.post("/api/v1/catalogs", json=ADDED)

        record = await _apply(client)

        assert record["stage"] == "incident"
        assert record["rollback"] == "failed"
        assert record["operator_message"] == INCIDENT_MESSAGE

        state = (await client.get("/api/v1/admin/maintenance-mode")).json()
        assert state["engaged"] is True
        assert state["engaged_by"] == "auto_rollback_failure"

        refused = await client.post("/api/v1/catalogs", json=KEPT)
        assert refused.status_code == 409
        assert refused.json()["code"] == "maintenance_mode"

        # Reads continue: diagnosing is what an Operator needs to do now.
        assert (await client.get("/api/v1/catalogs")).status_code == 200


async def _live_catalogs(forward: PortForward) -> set[str]:
    from app.adapters.trino import Trino

    return await Trino(host="127.0.0.1", port=forward.port).catalogs()

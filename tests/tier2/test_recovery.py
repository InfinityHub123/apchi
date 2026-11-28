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


async def test_reverts_and_a_rollback_on_a_real_cluster(
    e2e_client: AsyncClient, forward: PortForward, real_kubernetes: RealKubernetes
) -> None:
    """One history, recovered three ways. One scenario, because each recovery here is an Apply
    and each test pays a restart again to be put back (#300).

    - **A catalog revert drops for real, and the drop survives the pod.** Removing a catalog
      from Trino while leaving it in the Secret would bring it back at the next restart.
    - **Reverting a rollout-required Section restarts the Cluster and says so first.** Both
      reverts land in one Apply, so one restart.
    - **A full rollback restores a Snapshot without modifying it.** Its content becomes a new
      Snapshot's; the old one is untouched.
    """
    from app.sections.resource_groups.generator import RULES_KEY
    from tests.conftest import RESTART_SECTION, change_the_restart, stage_a_restart
    from tests.tier2.conftest import RESOURCE_GROUPS_SECRET

    def live() -> Trino:
        return Trino(url=f"http://127.0.0.1:{forward.port}")

    await e2e_client.post("/api/v1/catalogs", json=ALPHA)
    await stage_a_restart(e2e_client, limit=10)
    await _apply(e2e_client)
    forward.restart()
    await e2e_client.post("/api/v1/catalogs", json=BETA)
    await change_the_restart(e2e_client, limit=20)
    await _apply(e2e_client)
    forward.restart()

    catalogs = (await e2e_client.post("/api/v1/catalogs/revert", json={"snapshot": 1})).json()
    assert catalogs["catalogs_dropped"] == ["beta"]
    groups = (await e2e_client.post("/api/v1/resource-groups/revert", json={"snapshot": 1})).json()
    assert groups["cost"]["restarts_coordinator"] is True
    record = await _apply(e2e_client)
    forward.restart()

    assert record["snapshot"] == 3
    assert record["rolled_out"] is True
    catalogs_live = await live().catalogs()
    assert "alpha" in catalogs_live
    assert "beta" not in catalogs_live
    assert set(await real_kubernetes.read_secret(CATALOG_SEED_SECRET)) == {"alpha.properties"}
    delivered = await real_kubernetes.read_secret(RESOURCE_GROUPS_SECRET)
    assert '"hardConcurrencyLimit": 10' in delivered[RULES_KEY]
    first = (await e2e_client.get("/api/v1/snapshots/1")).json()
    third = (await e2e_client.get("/api/v1/snapshots/3")).json()
    assert third["sections"][RESTART_SECTION] == first["sections"][RESTART_SECTION]

    restart_coordinator()
    forward.restart()
    assert "beta" not in await live().catalogs()

    await e2e_client.post("/api/v1/catalogs", json=BETA)
    await _apply(e2e_client)
    await e2e_client.post("/api/v1/candidate/rollback", json={"snapshot": 1})
    record = await _apply(e2e_client)

    assert record["snapshot"] == 5
    restored = (await e2e_client.get("/api/v1/snapshots/5")).json()
    assert restored["sections"] == first["sections"]
    assert first["apply_id"] != restored["apply_id"]
    assert (await e2e_client.get("/api/v1/snapshots/1")).json() == first
    assert "beta" not in await live().catalogs()

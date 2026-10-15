"""Resource Groups against a real cluster.

Tier 1 proves Apchi writes two files and asks for the Rollout. Only a real coordinator
proves the file is one Trino accepts -- and that it comes back on it, which is the whole
risk of a Section whose configuration is read once at startup.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.adapters.trino import Trino
from app.pipeline.applies import TERMINAL
from tests.tier2.conftest import PortForward, coordinator_log

pytestmark = pytest.mark.tier2

SETTINGS = {"cpu_quota_period": "1h"}
GLOBAL = {"path": "global", "hard_concurrency_limit": 100, "max_queued": 1000}
ETL = {
    "path": "global.etl",
    "hard_concurrency_limit": 10,
    "max_queued": 100,
    "soft_memory_limit": "30%",
    "scheduling_policy": "weighted",
    "scheduling_weight": 3,
    "soft_cpu_limit": "30m",
    "hard_cpu_limit": "1h",
}
ADHOC = {"path": "global.adhoc", "hard_concurrency_limit": 20, "scheduling_weight": 1}
SELECTORS = {
    "selectors": [
        {"user": "etl_.*", "query_type": "SELECT", "group": "global.etl"},
        {"group": "global.adhoc"},
    ]
}


async def _validation(client: AsyncClient, timeout: float = 600.0) -> dict:
    started = await client.post("/api/v1/validations")
    assert started.status_code == 202
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/validations/{started.json()['id']}")).json()
        if record["outcome"] != "running":
            return record
        await asyncio.sleep(1)
    raise AssertionError("Validation never finished")


async def _apply(client: AsyncClient, timeout: float = 900.0) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(1)
    raise AssertionError("Apply never settled")


async def _stage(client: AsyncClient) -> None:
    await client.put("/api/v1/resource-groups/settings", json=SETTINGS)
    for group in (GLOBAL, ETL, ADHOC):
        created = await client.post("/api/v1/resource-groups", json=group)
        assert created.status_code == 201, created.json()
    await client.put("/api/v1/resource-groups/selectors", json=SELECTORS)


async def test_a_real_coordinator_comes_back_on_the_generated_file(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """Every field Apchi models, in one hierarchy, on a coordinator that has to restart to
    read it. A file Trino will not parse is a coordinator that never comes back."""
    await _stage(e2e_client)

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    forward.restart()
    trino = Trino(host="127.0.0.1", port=forward.port)
    assert await trino.query("SELECT 1") == [[1]]
    assert "SERVER STARTED" in coordinator_log(400)


async def test_the_queries_land_somewhere_the_coordinator_reports(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """The manager is live, not merely parsed: Trino records the group a query ran in."""
    await _stage(e2e_client)
    await _apply(e2e_client)
    forward.restart()

    trino = Trino(host="127.0.0.1", port=forward.port)
    await trino.query("SELECT 1")
    groups = await trino.query(
        "SELECT DISTINCT resource_group_id FROM system.runtime.queries "
        "WHERE resource_group_id IS NOT NULL"
    )

    assert groups, "the coordinator attributed no query to a resource group"


async def test_a_file_trino_will_not_parse_fails_validation_before_the_cluster_is_touched(
    e2e_client: AsyncClient,
) -> None:
    """A memory limit is a free-form string to Apchi and a parsed quantity to Trino: "size is
    not a valid data size string: nonsense"."""
    await e2e_client.post(
        "/api/v1/resource-groups",
        json={"path": "global", "hard_concurrency_limit": 1, "soft_memory_limit": "nonsense"},
    )
    await e2e_client.put(
        "/api/v1/resource-groups/selectors", json={"selectors": [{"group": "global"}]}
    )

    verdict = await _validation(e2e_client)

    assert verdict["outcome"] == "failed"
    assert verdict["failures"][0]["reason"], "Trino's own words, which exist only in the pod log"


async def test_emptying_the_section_leaves_a_coordinator_that_still_starts(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """Both files go together. Leaving the properties file behind would point Trino at a
    file that is no longer mounted, and it refuses to start on that."""
    await _stage(e2e_client)
    await _apply(e2e_client)

    for path in ("global.etl", "global.adhoc", "global"):
        await e2e_client.delete(f"/api/v1/resource-groups/{path}")
    await e2e_client.put("/api/v1/resource-groups/selectors", json={"selectors": []})
    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    forward.restart()
    trino = Trino(host="127.0.0.1", port=forward.port)
    assert await trino.query("SELECT 1") == [[1]]

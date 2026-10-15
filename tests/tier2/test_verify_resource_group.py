"""Verification proving the resource group configuration is in force, on a real cluster.

Tier 1 can prove the check fails when the Cluster disagrees, because its Trino runs no
resource group manager and files everything under `global`. Only a real cluster can prove
the other half: that a coordinator which *did* adopt the file agrees with the prediction, so
this check passes on every healthy Apply rather than blocking them all.

A selector routing Apchi's own identity is what the ticket asks for, and it is also what
makes the prediction certain: Apchi knows its own user and the source it sends.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.adapters.trino import SOURCE, Trino
from app.pipeline.applies import TERMINAL
from tests.tier2.conftest import PortForward

pytestmark = pytest.mark.tier2

VERIFICATION = {"path": "verification", "hard_concurrency_limit": 5}
EVERYTHING = {"path": "everything", "hard_concurrency_limit": 20}


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


async def test_a_selector_routing_apchi_is_verified_on_the_real_cluster(
    e2e_client: AsyncClient, forward: PortForward, settings
) -> None:
    """The Apply passes because the coordinator really did adopt the file: the query landed
    in the group the selectors named, and Verification read that back from Trino."""
    for group in (VERIFICATION, EVERYTHING):
        created = await e2e_client.post("/api/v1/resource-groups", json=group)
        assert created.status_code == 201, created.json()
    await e2e_client.put(
        "/api/v1/resource-groups/selectors",
        json={
            "selectors": [
                {"user": settings.trino_user, "source": SOURCE, "group": "verification"},
                {"group": "everything"},
            ]
        },
    )

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    forward.restart()
    trino = Trino(host="127.0.0.1", port=forward.port, user=settings.trino_user)
    ran = await trino.run("SELECT 1")
    group = await trino.query(
        f"SELECT resource_group_id FROM system.runtime.queries WHERE query_id = '{ran.query_id}'"
    )
    assert group[0][0] == ["verification"], "Apchi's own queries are routed by its own source"


async def test_another_identity_falls_through_to_the_operators_own_rule(
    e2e_client: AsyncClient, forward: PortForward, settings
) -> None:
    """Apchi's reserved-looking rule is nothing of the kind: it is the Operator's own
    selector, and everyone else goes where the rules beneath it say."""
    for group in (VERIFICATION, EVERYTHING):
        await e2e_client.post("/api/v1/resource-groups", json=group)
    await e2e_client.put(
        "/api/v1/resource-groups/selectors",
        json={
            "selectors": [
                {"user": settings.trino_user, "group": "verification"},
                {"group": "everything"},
            ]
        },
    )

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    forward.restart()
    other = Trino(host="127.0.0.1", port=forward.port, user="someone")
    ran = await other.run("SELECT 1")
    group = await other.query(
        f"SELECT resource_group_id FROM system.runtime.queries WHERE query_id = '{ran.query_id}'"
    )
    assert group[0][0] == ["everything"]

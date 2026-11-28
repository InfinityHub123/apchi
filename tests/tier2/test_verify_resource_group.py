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


async def test_selectors_route_apchi_and_everyone_else_on_the_real_cluster(
    e2e_client: AsyncClient, forward: PortForward, settings
) -> None:
    """One Apply and both sides of the selector. One scenario, because the Apply restarts the
    coordinator and each test pays a restart again to be put back (#300).

    - **Apchi's own queries are routed by its own source**, and the Apply passes because the
      coordinator really did adopt the file: Verification read the group back from Trino.
    - **Another identity falls through to the Operator's own rule.** Apchi's rule is nothing
      reserved: it is the Operator's selector, and everyone else goes where the rules beneath
      it say.
    """
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

    async def group_of(user: str) -> list[str]:
        trino = Trino(url=f"http://127.0.0.1:{forward.port}", user=user)
        ran = await trino.run("SELECT 1")
        rows = await trino.query(
            "SELECT resource_group_id FROM system.runtime.queries "
            f"WHERE query_id = '{ran.query_id}'"
        )
        return list(rows[0][0])

    assert await group_of(settings.trino_user) == ["verification"]
    assert await group_of("someone") == ["everything"]

"""A whole Apply against a live cluster: DDL to a real coordinator, a real Secret
patch, real Verification, a committed Snapshot.

The case that justifies this tier is the restart. Tier 1 can prove Apchi writes the
Secret, but only a real pod can prove the catalog is still there after the pod that
held it is gone -- which is the entire reason Apply writes twice.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.adapters.kubernetes import RealKubernetes
from app.adapters.trino import Trino
from tests.tier2.conftest import CATALOG_SEED_SECRET, PortForward, restart_coordinator

pytestmark = pytest.mark.tier2

MEMORY = {"name": "scratch", "connector": "memory", "properties": {}}


async def _settled(client: AsyncClient, apply_id: str, timeout: float = 300.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in {"succeeded", "failed"}:
            return record
        await asyncio.sleep(0.2)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


async def _apply(client: AsyncClient) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202
    record = await _settled(client, started.json()["id"])
    assert record["stage"] == "succeeded", record.get("failure_reason")
    return record


async def test_a_catalog_applied_restarted_dropped_and_restarted(
    e2e_client: AsyncClient, forward: PortForward, real_kubernetes: RealKubernetes
) -> None:
    """A catalog's whole life on a real coordinator. One scenario, because the restarts are
    the expensive part and each test paid its own (#300).

    - **An Apply reaches a real coordinator and commits a Snapshot**, and the seed Secret
      holds what was applied.
    - **It survives a coordinator restart.** CREATE CATALOG writes the coordinator's store,
      an emptyDir, so the pod takes it to the grave; it comes back only because Apply also
      patched the Secret the initContainer seeds from.
    - **Dropped, it does not come back after a restart.** The one a merging Secret patch
      gets wrong: dropping has to remove the key, or the next restart seeds it back in.
    """

    async def live() -> set[str]:
        return set(await Trino(url=f"http://127.0.0.1:{forward.port}").catalogs())

    await e2e_client.post("/api/v1/catalogs", json=MEMORY)

    record = await _apply(e2e_client)

    assert record["snapshot"] == 1
    assert "scratch" in await live()
    seed = await real_kubernetes.read_secret(CATALOG_SEED_SECRET)
    assert seed["scratch.properties"] == "connector.name=memory\n"

    restart_coordinator()
    forward.restart()
    assert "scratch" in await live()

    await e2e_client.delete("/api/v1/catalogs/scratch")
    await _apply(e2e_client)
    restart_coordinator()
    forward.restart()
    assert "scratch" not in await live()

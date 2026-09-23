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


async def test_an_apply_reaches_a_real_coordinator_and_commits_a_snapshot(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    await e2e_client.post("/api/v1/catalogs", json=MEMORY)

    record = await _apply(e2e_client)

    assert record["snapshot"] == 1
    live = await Trino(host="127.0.0.1", port=forward.port).catalogs()
    assert "scratch" in live


async def test_an_applied_catalog_survives_a_coordinator_restart(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """The vanishing catalog. CREATE CATALOG writes the coordinator's store, which is
    an emptyDir, so the pod takes it to the grave. The catalog comes back only
    because Apply also patched the Secret the initContainer seeds from.
    """
    await e2e_client.post("/api/v1/catalogs", json=MEMORY)
    await _apply(e2e_client)

    restart_coordinator()
    forward.restart()

    live = await Trino(host="127.0.0.1", port=forward.port).catalogs()
    assert "scratch" in live


async def test_a_dropped_catalog_does_not_come_back_after_a_restart(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """The inverse, and the one a merging Secret patch gets wrong: dropping the
    catalog has to remove its key from the Secret, or the next restart seeds it
    straight back in."""
    await e2e_client.post("/api/v1/catalogs", json=MEMORY)
    await _apply(e2e_client)

    await e2e_client.delete("/api/v1/catalogs/scratch")
    await _apply(e2e_client)

    restart_coordinator()
    forward.restart()

    live = await Trino(host="127.0.0.1", port=forward.port).catalogs()
    assert "scratch" not in live


async def test_the_seed_secret_holds_what_was_applied(
    e2e_client: AsyncClient, real_kubernetes: RealKubernetes
) -> None:
    await e2e_client.post("/api/v1/catalogs", json=MEMORY)
    await _apply(e2e_client)

    seed = await real_kubernetes.read_secret(CATALOG_SEED_SECRET)

    assert seed["scratch.properties"] == "connector.name=memory\n"

"""A configuration exported from a live cluster, imported back, and applied.

Tier 1 already proves the document's shape and every refusal. What only a real cluster can
prove is that the thing a document carries is still usable after the round trip: an import
replaces the Candidate wholesale, and an Apply then has to patch real Secrets, reach a real
coordinator and pass real Verification with nothing but what came out of the file.

The case that justifies the cost is a catalog's credentials. Snapshots store actual secret
values rather than references (section 12), so if an export dropped or mangled one, the
import would succeed, the Apply would succeed, and the catalog would be the thing that
failed -- later, on a query, for a reason nothing connects to the import.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.adapters.trino import Trino
from tests.tier2.conftest import PortForward

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


async def test_an_exported_snapshot_imports_and_applies_to_a_real_cluster(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """Export a committed Snapshot, throw the Candidate away, import the document, apply.

    The second Apply has nothing to go on but the file, so a catalog reaching the
    coordinator is the document having carried everything that catalog needs.
    """
    await e2e_client.post("/api/v1/catalogs", json=MEMORY)
    first = await _apply(e2e_client)
    number = first["snapshot"]

    exported = await e2e_client.get(f"/api/v1/snapshots/{number}/export")
    assert exported.status_code == 200, exported.json()

    assert (await e2e_client.delete("/api/v1/catalogs/scratch")).status_code == 204
    emptied = await _apply(e2e_client)
    assert "scratch" not in await Trino(host="127.0.0.1", port=forward.port).catalogs()

    imported = await e2e_client.put("/api/v1/candidate", json=exported.json())
    assert imported.status_code == 200, imported.json()
    assert imported.json()["has_changes"] is True, (
        "the import is a change from the emptied Snapshot, which is what Review must say"
    )

    restored = await _apply(e2e_client)
    assert restored["snapshot"] == emptied["snapshot"] + 1, (
        "an import produces a new Snapshot rather than rewriting the one it came from"
    )
    assert "scratch" in await Trino(host="127.0.0.1", port=forward.port).catalogs()

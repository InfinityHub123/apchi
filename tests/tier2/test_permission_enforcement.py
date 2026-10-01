"""Grants that mean something, on a real cluster.

Tier 1 proves Apchi writes the right file. Only a real coordinator proves the thing an Admin
is actually deciding: that after this, an identity reaches what it was granted and nothing
else -- and that Apchi itself still works, which is what makes the decision recoverable.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.adapters.trino import Trino
from app.pipeline.applies import TERMINAL
from tests.tier2.conftest import PortForward

pytestmark = pytest.mark.tier2

ENFORCEMENT = "/api/v1/admin/permissions/enforcement"

#: Staged by the test, because an Apply renders the catalog seed from the Candidate.
BENCH = {"name": "bench", "connector": "tpch", "properties": {}}
GRANT = {
    "identity": "alice",
    "catalog": "bench",
    "schema": "tiny",
    "table": "nation",
    "privileges": ["SELECT"],
}

#: Trino re-reads the rules on the dev cluster's one-second refresh period, and the kubelet
#: has to project the Secret first. Polled rather than slept.
_ENFORCED_WITHIN = 150.0


async def _apply(
    client: AsyncClient, path: str = "/api/v1/applies", timeout: float = 900.0
) -> dict:
    started = await client.post(path)
    assert started.status_code == 202, started.json()
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(1)
    raise AssertionError("Apply never settled")


async def _allowed(trino: Trino, sql: str) -> bool:
    try:
        await trino.query(sql)
        return True
    except Exception:
        return False


async def _until(trino: Trino, sql: str, allowed: bool) -> bool:
    deadline = asyncio.get_running_loop().time() + _ENFORCED_WITHIN
    while asyncio.get_running_loop().time() < deadline:
        if await _allowed(trino, sql) is allowed:
            return True
        await asyncio.sleep(2)
    return False


async def test_closing_the_cluster_enforces_the_grants(
    e2e_client: AsyncClient, forward: PortForward, settings
) -> None:
    """The whole decision, on a real coordinator: granted access works, ungranted access
    stops, and Apchi still reaches the Cluster it has to recover."""
    assert (await e2e_client.post("/api/v1/catalogs", json=BENCH)).status_code == 201
    assert (await e2e_client.post("/api/v1/permissions", json=GRANT)).status_code == 201
    record = await _apply(e2e_client)
    assert record["stage"] == "succeeded", record

    alice = Trino(host="127.0.0.1", port=forward.port, user="alice")
    # Open: she can read a table nobody granted her.
    assert await _until(alice, "SELECT count(*) FROM bench.tiny.region", allowed=True)

    await e2e_client.put(ENFORCEMENT, json={"enforced": True})
    record = await _apply(e2e_client, "/api/v1/admin/applies")
    assert record["stage"] == "succeeded", record

    # Closed: only what she was granted.
    assert await _until(alice, "SELECT count(*) FROM bench.tiny.region", allowed=False)
    assert await _allowed(alice, "SELECT count(*) FROM bench.tiny.nation")
    # And Apchi, which is what makes this recoverable rather than a one-way door.
    apchi = Trino(host="127.0.0.1", port=forward.port, user=settings.trino_user)
    assert await _allowed(apchi, "SELECT 1 FROM system.runtime.nodes LIMIT 1")
    assert await _allowed(apchi, "SELECT count(*) FROM system.runtime.queries")


async def test_an_identity_with_no_grant_at_all_is_denied(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """What an Admin is signing up to: everyone they have not granted loses what they had."""
    await e2e_client.post("/api/v1/catalogs", json=BENCH)
    await e2e_client.post("/api/v1/permissions", json=GRANT)
    await e2e_client.put(ENFORCEMENT, json={"enforced": True})
    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    stranger = Trino(host="127.0.0.1", port=forward.port, user="nobody")
    assert await _until(stranger, "SELECT count(*) FROM bench.tiny.nation", allowed=False)
    # Still able to run a query at all -- the queries block grants execute to everyone.
    assert await _allowed(stranger, "SELECT 1")


async def test_reopening_the_cluster_restores_what_was_there(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """An Admin who closes a Cluster too early has to be able to undo it, and the posture is
    a value rather than a migration step for exactly that reason."""
    await e2e_client.post("/api/v1/catalogs", json=BENCH)
    await e2e_client.put(ENFORCEMENT, json={"enforced": True})
    await _apply(e2e_client)
    stranger = Trino(host="127.0.0.1", port=forward.port, user="nobody")
    assert await _until(stranger, "SELECT count(*) FROM bench.tiny.nation", allowed=False)

    await e2e_client.put(ENFORCEMENT, json={"enforced": False})
    record = await _apply(e2e_client, "/api/v1/admin/applies")

    assert record["stage"] == "succeeded", record
    assert await _until(stranger, "SELECT count(*) FROM bench.tiny.nation", allowed=True)

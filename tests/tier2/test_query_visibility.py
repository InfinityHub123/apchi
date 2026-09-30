"""The system-owned queries block, against a real cluster.

Every claim in this block was surprising enough to need a real coordinator to settle, so
this is where it is settled. By default any authenticated End User can view and kill any
query, and query text routinely contains data; what Apchi generates has to close that
without closing the Cluster.
"""

import asyncio

import httpx
import pytest
from httpx import AsyncClient

from app.adapters.trino import Trino
from app.pipeline.applies import TERMINAL
from tests.tier2.conftest import PortForward

pytestmark = pytest.mark.tier2

#: A grant is what makes Apchi know an identity, which is what earns it a kill rule.
GRANT = {"identity": "alice", "catalog": "tpch", "schema": "tiny", "privileges": ["SELECT"]}


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


async def _visible_to(port: int, user: str) -> set[str]:
    trino = Trino(host="127.0.0.1", port=port, user=user)
    return {row[0] for row in await trino.query("SELECT query_id FROM system.runtime.queries")}


async def test_end_users_can_still_run_queries(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """The block is all-or-nothing, so the first thing to prove is that it did not stop the
    Cluster serving."""
    await e2e_client.post("/api/v1/permissions", json=GRANT)

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    alice = Trino(host="127.0.0.1", port=forward.port, user="alice")
    assert await alice.query("SELECT 1") == [[1]]


async def test_one_end_user_cannot_see_anothers_query(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """The leak this block exists to close. Query text routinely contains data."""
    await e2e_client.post("/api/v1/permissions", json=GRANT)
    record = await _apply(e2e_client)
    assert record["stage"] == "succeeded", record

    alice = Trino(host="127.0.0.1", port=forward.port, user="alice")
    bob = Trino(host="127.0.0.1", port=forward.port, user="bob")
    hers = (await alice.run("SELECT 'alice ran this'")).query_id
    his = (await bob.run("SELECT 'bob ran this'")).query_id

    assert hers in await _visible_to(forward.port, "alice"), "her own, which needs no rule"
    assert hers not in await _visible_to(forward.port, "bob")
    assert his not in await _visible_to(forward.port, "alice")


async def test_apchi_sees_every_query_so_the_running_count_is_honest(
    e2e_client: AsyncClient, forward: PortForward, settings
) -> None:
    """Trino filters these rows by who may view them. Without Apchi's own rule the count
    Review shows before a Rollout would only ever be Apchi's own queries."""
    await e2e_client.post("/api/v1/permissions", json=GRANT)
    await _apply(e2e_client)

    alice = Trino(host="127.0.0.1", port=forward.port, user="alice")
    hers = (await alice.run("SELECT 'alice ran this'")).query_id

    assert hers in await _visible_to(forward.port, settings.trino_user)
    review = (await e2e_client.get("/api/v1/review")).json()
    assert review["cost"]["queries_at_risk"] is None, "no restart is coming, so nothing counted"


async def test_an_identity_apchi_knows_can_kill_its_own_query_and_nobody_elses(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """The half of §13.4 that needed a rule per identity: killing your own query is not
    implicit, and `queryOwner` has no back-reference to the requesting user.

    Killed through Trino's own endpoint rather than `system.runtime.kill_query`: the
    procedure is a separate permission, denied to everyone since Apchi first wrote an
    access-control file.
    """
    await e2e_client.post("/api/v1/permissions", json=GRANT)
    await _apply(e2e_client)

    alice = Trino(host="127.0.0.1", port=forward.port, user="alice")
    long_query = asyncio.create_task(alice.query("SELECT count(*) FROM tpch.sf100.lineitem"))
    await asyncio.sleep(8)
    running = await Trino(host="127.0.0.1", port=forward.port).query(
        "SELECT query_id FROM system.runtime.queries WHERE \"user\" = 'alice' AND state = 'RUNNING'"
    )
    assert running, "alice's query should still be running"
    query_id = running[0][0]

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{forward.port}", timeout=10) as http:
        refused = await http.delete(f"/v1/query/{query_id}", headers={"X-Trino-User": "bob"})
        allowed = await http.delete(f"/v1/query/{query_id}", headers={"X-Trino-User": "alice"})

    assert refused.status_code == 403, "bob may not kill hers"
    assert allowed.status_code == 204, "she may kill her own"
    long_query.cancel()

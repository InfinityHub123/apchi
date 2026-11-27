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
GRANT = {"identity": "alice", "catalog": "bench", "schema": "tiny", "privileges": ["SELECT"]}

#: Staged by the test rather than assumed. An Apply renders the catalog seed Secret from the
#: Candidate, so a Cluster whose coordinator has restarted since some earlier Apply has only
#: the catalogs the Candidate names -- which is exactly what it should have, and why a test
#: that wants a catalog has to bring it.
BENCH = {"name": "bench", "connector": "tpch", "properties": {}}


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


async def _running_query_of(trino: Trino, user: str, within: float = 60.0) -> str | None:
    """Polls for the query rather than sleeping and hoping: a fixed sleep was flaky in both
    directions, too short and it has not started, too long and it has finished."""
    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        rows = await trino.query(
            "SELECT query_id FROM system.runtime.queries "
            f"WHERE \"user\" = '{user}' AND state = 'RUNNING'"
        )
        if rows:
            return str(rows[0][0])
        await asyncio.sleep(1)
    return None


async def _visible_to(port: int, user: str) -> set[str]:
    trino = Trino(url=f"http://127.0.0.1:{port}", user=user)
    return {row[0] for row in await trino.query("SELECT query_id FROM system.runtime.queries")}


async def test_the_queries_block_on_a_real_cluster(
    e2e_client: AsyncClient, forward: PortForward, settings
) -> None:
    """One Apply of the block, and every claim it makes. One scenario, because a changed rules
    file costs each test a coordinator restart to be put back (#300).

    - **End users can still run queries.** The block is all-or-nothing, so the first thing to
      prove is that it did not stop the Cluster serving.
    - **One end user cannot see another's query** -- the leak this block exists to close;
      query text routinely contains data.
    - **Apchi sees every query, so the running count is honest.** Trino filters these rows by
      who may view them; without Apchi's own rule the count Review shows before a Rollout
      would only ever be Apchi's own queries.
    - **An identity Apchi knows can kill its own query and nobody else's.** Killing your own
      is not implicit, and `queryOwner` has no back-reference to the requesting user. Killed
      through Trino's own endpoint, and then through `system.runtime.kill_query`, the
      procedure: granting it must not widen an authority, so whose query may be killed is
      still this block's answer.
    """
    await e2e_client.post("/api/v1/permissions", json=GRANT)
    created = await e2e_client.post("/api/v1/catalogs", json=BENCH)
    assert created.status_code == 201, created.json()

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    alice = Trino(url=f"http://127.0.0.1:{forward.port}", user="alice")
    bob = Trino(url=f"http://127.0.0.1:{forward.port}", user="bob")
    assert await alice.query("SELECT 1") == [[1]]

    hers = (await alice.run("SELECT 'alice ran this'")).query_id
    his = (await bob.run("SELECT 'bob ran this'")).query_id
    assert hers in await _visible_to(forward.port, "alice"), "her own, which needs no rule"
    assert hers not in await _visible_to(forward.port, "bob")
    assert his not in await _visible_to(forward.port, "alice")
    assert hers in await _visible_to(forward.port, settings.trino_user)
    review = (await e2e_client.get("/api/v1/review")).json()
    assert review["cost"]["queries_at_risk"] is None, "no restart is coming, so nothing counted"

    long_query = asyncio.create_task(alice.query("SELECT count(*) FROM bench.sf100.lineitem"))
    await asyncio.sleep(8)
    running = await Trino(url=f"http://127.0.0.1:{forward.port}").query(
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

    # sf1000 so the query cannot finish before the test has looked at it.
    long_query = asyncio.create_task(alice.query("SELECT count(*) FROM bench.sf1000.lineitem"))
    apchi = Trino(url=f"http://127.0.0.1:{forward.port}", user=settings.trino_user)
    query_id = await _running_query_of(apchi, "alice")
    assert query_id, "alice's query should still be running"
    with pytest.raises(Exception, match="(?i)cannot kill"):
        await bob.query(
            f"CALL system.runtime.kill_query(query_id => '{query_id}', message => 'not his')"
        )
    killed = await alice.query(
        f"CALL system.runtime.kill_query(query_id => '{query_id}', message => 'hers to kill')"
    )
    assert killed is not None
    long_query.cancel()

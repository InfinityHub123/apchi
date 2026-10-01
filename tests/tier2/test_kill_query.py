"""Killing your own query, on a real cluster.

Apchi took this away the day it was installed, and nobody noticed for three slices: a
file-based access control denies procedure execution unless a rule allows it, and Apchi has
written such a file since slice 1. `CALL system.runtime.kill_query(...)` failed for
everyone, including somebody killing their own query.

Two things have to be true at once here, which is why it is worth a test on a real
coordinator: the procedure has to be callable, and the queries block has to go on deciding
whose query may be killed.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.adapters.trino import Trino
from app.pipeline.applies import TERMINAL
from tests.tier2.conftest import PortForward

pytestmark = pytest.mark.tier2

#: Staged by the test: an Apply renders the catalog seed from the Candidate, so a test that
#: wants a catalog to run a long query against has to bring one.
BENCH = {"name": "bench", "connector": "tpch", "properties": {}}

#: A grant is what makes Apchi know an identity, which is what earns it a kill rule (§13.4).
GRANT = {"identity": "alice", "catalog": "bench", "schema": "tiny", "privileges": ["SELECT"]}


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
    """Polls for the query rather than sleeping and hoping.

    A fixed sleep made this flaky in both directions: too short and the query has not
    started, too long and it has finished. Neither is what the test is about.
    """
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


async def test_an_identity_can_kill_its_own_query_through_sql(
    e2e_client: AsyncClient, forward: PortForward, settings
) -> None:
    """The regression, closed. Through the procedure rather than Trino's own endpoint --
    which is the half that was broken."""
    assert (await e2e_client.post("/api/v1/catalogs", json=BENCH)).status_code == 201
    assert (await e2e_client.post("/api/v1/permissions", json=GRANT)).status_code == 201
    record = await _apply(e2e_client)
    assert record["stage"] == "succeeded", record

    alice = Trino(host="127.0.0.1", port=forward.port, user="alice")
    # sf1000 so the query cannot finish before the test has looked at it.
    long_query = asyncio.create_task(alice.query("SELECT count(*) FROM bench.sf1000.lineitem"))
    apchi = Trino(host="127.0.0.1", port=forward.port, user=settings.trino_user)
    query_id = await _running_query_of(apchi, "alice")
    assert query_id, "alice's query should still be running"

    killed = await alice.query(
        f"CALL system.runtime.kill_query(query_id => '{query_id}', message => 'hers to kill')"
    )

    assert killed == [] or killed is not None
    long_query.cancel()


async def test_one_identity_cannot_kill_anothers_query_through_sql(
    e2e_client: AsyncClient, forward: PortForward, settings
) -> None:
    """Granting the procedure must not widen an authority. Whose query may be killed is
    still the queries block's answer."""
    await e2e_client.post("/api/v1/catalogs", json=BENCH)
    await e2e_client.post("/api/v1/permissions", json=GRANT)
    record = await _apply(e2e_client)
    assert record["stage"] == "succeeded", record

    alice = Trino(host="127.0.0.1", port=forward.port, user="alice")
    # sf1000 so the query cannot finish before the test has looked at it.
    long_query = asyncio.create_task(alice.query("SELECT count(*) FROM bench.sf1000.lineitem"))
    apchi = Trino(host="127.0.0.1", port=forward.port, user=settings.trino_user)
    query_id = await _running_query_of(apchi, "alice")
    assert query_id, "alice's query should still be running"

    bob = Trino(host="127.0.0.1", port=forward.port, user="bob")
    with pytest.raises(Exception, match="(?i)cannot kill"):
        await bob.query(
            f"CALL system.runtime.kill_query(query_id => '{query_id}', message => 'not his')"
        )

    long_query.cancel()

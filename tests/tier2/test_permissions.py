"""Grants against a real cluster.

Tier 1 proves Apchi writes the rules. Only a real coordinator proves the two things that
matter about this Section: that Trino picks the rules up **without being restarted**, and
that what Apchi generated is a file it enforces the way an Operator meant.

The dev cluster sets `security.refresh-period` to a second, so the wait here is short. On a
Cluster where it is not set at all Trino would read the rules once at startup and never
again, which is why that is becoming a precondition of its own.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.adapters.trino import Trino
from app.pipeline.applies import TERMINAL
from tests.tier2.conftest import PortForward

pytestmark = pytest.mark.tier2

#: A grant on a table that exists on the dev cluster, so it can be read for real.
READ_NATION = {
    "identity": "alice",
    "catalog": "tpch",
    "schema": "tiny",
    "table": "nation",
    "privileges": ["SELECT"],
}

#: How long to wait for Trino to re-read the rules: the kubelet's projection plus the
#: refresh period, generously. The successful path is normally a few seconds.
_ENFORCED_WITHIN = 120.0


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


async def _answers(trino: Trino, sql: str) -> bool:
    try:
        await trino.query(sql)
        return True
    except Exception:
        return False


async def _until(trino: Trino, sql: str, allowed: bool) -> bool:
    """Polls until the Cluster's answer matches, which is what Apply itself will do once the
    propagation wait lands."""
    deadline = asyncio.get_running_loop().time() + _ENFORCED_WITHIN
    while asyncio.get_running_loop().time() < deadline:
        if await _answers(trino, sql) is allowed:
            return True
        await asyncio.sleep(2)
    return False


async def test_a_grant_is_enforced_without_restarting_the_coordinator(
    e2e_client: AsyncClient, forward: PortForward, real_kubernetes
) -> None:
    """The whole point of this Section's engine: Trino re-reads the rules on a timer, so a
    permission change reaches a busy Cluster without destroying a single query."""
    before = await real_kubernetes.rollout_state("trino-coordinator")
    await e2e_client.post("/api/v1/permissions", json=READ_NATION)

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    after = await real_kubernetes.rollout_state("trino-coordinator")
    assert after.generation == before.generation, "the pod template was never touched"

    alice = Trino(host="127.0.0.1", port=forward.port, user="alice")
    assert await _until(alice, "SELECT count(*) FROM tpch.tiny.nation", allowed=True)


async def test_the_generated_file_is_one_a_real_trino_enforces_as_written(
    e2e_client: AsyncClient, forward: PortForward, settings
) -> None:
    """A grant on one table is a grant on that table. The names Apchi anchors are why: an
    unanchored pattern would have granted more than the Operator wrote."""
    await e2e_client.post("/api/v1/permissions", json=READ_NATION)

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    alice = Trino(host="127.0.0.1", port=forward.port, user="alice")
    assert await _until(alice, "SELECT count(*) FROM tpch.tiny.nation", allowed=True)
    # Still allowed on everything else, because the catch-all is still there: staging a
    # grant records intent, it does not revoke anyone's access.
    assert await _answers(alice, "SELECT count(*) FROM tpch.tiny.region")
    # And Apchi can still read what Verification needs, which is the reserved rule's job.
    apchi = Trino(host="127.0.0.1", port=forward.port, user=settings.trino_user)
    assert await _answers(apchi, "SELECT 1 FROM system.runtime.nodes LIMIT 1")


async def test_rules_edited_outside_apchi_are_corrected_by_the_next_apply(
    e2e_client: AsyncClient, real_kubernetes, forward: PortForward, settings
) -> None:
    """Generated every Apply rather than written once, so a Cluster whose rules were edited
    behind Apchi's back does not quietly keep catalog DDL open to everyone."""
    await real_kubernetes.write_secret("trino-access-control", {"rules.json": '{"catalogs": []}'})

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    written = await real_kubernetes.read_secret("trino-access-control")
    assert "^apchi$" in written["rules.json"]

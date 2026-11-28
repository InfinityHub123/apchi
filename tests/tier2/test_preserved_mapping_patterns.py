"""A Cluster with preserved mapping patterns, against a real coordinator.

Tier 1 proves Apchi writes the merged file and skips the Snapshot. Only a real Trino proves
the point of the ticket: that a Cluster onboarded mid-migration comes back **serving all of
them**, so the clients that have not re-issued certificates yet keep working.
"""

import asyncio

import pytest
from httpx import AsyncClient
from trino.exceptions import TrinoUserError

from app.adapters.trino import Trino
from app.pipeline.applies import TERMINAL
from tests.tier2.conftest import PortForward

pytestmark = pytest.mark.tier2

#: Where the Cluster is going: the single convention Apchi models.
DESTINATION = {"pattern": "(.*)@example\\.com", "user": "$1"}

#: Where it is coming from: two conventions its clients still authenticate with.
LEGACY_CN = {"pattern": "CN=(.*?),.*", "user": "$1"}
LEGACY_HOST = {"pattern": "(.*)\\.clients\\.internal", "user": "$1", "case": "lower"}


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


async def test_a_cluster_mid_migration_and_the_admin_apply_that_onboards_it(
    e2e_client: AsyncClient, forward: PortForward
) -> None:
    """Three conventions on one coordinator, then an Admin narrowing them. One scenario,
    because each Apply restarts the coordinator and each test pays a restart again to be put
    back (#300).

    - **Every preserved pattern is served** alongside the one Apchi models, so no client is
      locked out while it migrates. Each identity is read out of an impersonation refusal:
      insecure authentication takes the principal and the session user from the same header,
      so a pattern that renames leaves the two disagreeing and Trino names the mapped
      identity when it refuses.
    - **An Admin Apply delivers them and commits no Snapshot.** An Admin onboarding a Cluster
      cannot be made to wait for an Operator, and what they did is recorded as an Apply
      rather than as a Snapshot (section 14).
    """
    await e2e_client.put("/api/v1/certificate-mapping", json=DESTINATION)
    await e2e_client.put(
        "/api/v1/admin/certificate-mapping/preserved",
        json={"patterns": [LEGACY_CN, LEGACY_HOST]},
    )

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    forward.restart()
    for presented, mapped in (
        ("alice@example.com", "alice"),
        ("CN=bob,OU=data,O=acme", "bob"),
        ("Carol.clients.internal", "carol"),
    ):
        client = Trino(url=f"http://127.0.0.1:{forward.port}", user=presented)
        with pytest.raises(TrinoUserError, match=f"User {mapped} cannot impersonate"):
            await client.query("SELECT current_user")

    before = (await e2e_client.get("/api/v1/snapshots")).json()["items"]
    await e2e_client.put(
        "/api/v1/admin/certificate-mapping/preserved", json={"patterns": [LEGACY_CN]}
    )

    record = await _apply(e2e_client, "/api/v1/admin/applies")

    assert record["stage"] == "succeeded", record
    assert record["snapshot"] is None
    assert (await e2e_client.get("/api/v1/snapshots")).json()["items"] == before
    forward.restart()
    bob = Trino(url=f"http://127.0.0.1:{forward.port}", user="CN=bob, OU=data, O=acme")
    with pytest.raises(TrinoUserError, match="User bob cannot impersonate"):
        await bob.query("SELECT current_user")
    carol = Trino(url=f"http://127.0.0.1:{forward.port}", user="Carol.clients.internal")
    assert await carol.query("SELECT current_user") == [["Carol.clients.internal"]], (
        "the pattern the Admin dropped no longer maps"
    )

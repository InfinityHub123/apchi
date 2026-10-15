"""Whether a grant means anything yet.

The generated rules end in a catch-all allowing everything to everyone, which is what a
Cluster did before Apchi was installed. Until that goes, staging a grant records intent and
takes nothing away. Removing it is an Admin value for the same reason the preserved mapping
patterns are: it is a migration with an irreversible-feeling morning after it.
"""

import asyncio
import json

from httpx import AsyncClient

from app.pipeline.applies import TERMINAL
from app.sections.permissions.generator import RULES_KEY, render_rules
from tests.conftest import FakeKubernetes

SECRET = "trino-access-control"
GRANT = {
    "identity": "acme_finance",
    "catalog": "iceberg",
    "schema": "finance",
    "privileges": ["SELECT"],
}
ENFORCEMENT = "/api/v1/admin/permissions/enforcement"


async def _apply(
    client: AsyncClient, path: str = "/api/v1/applies", timeout: float = 180.0
) -> dict:
    started = await client.post(path)
    assert started.status_code == 202, started.json()
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError("Apply never settled")


def _tables(kubernetes: FakeKubernetes) -> list[dict]:
    return json.loads(kubernetes.secrets[SECRET][RULES_KEY])["tables"]


async def test_a_cluster_starts_open(client: AsyncClient) -> None:
    """Installing Apchi must not change who can reach what."""
    assert (await client.get(ENFORCEMENT)).json() == {"enforced": False}


def test_the_open_posture_ends_in_a_catch_all() -> None:
    rules = json.loads(render_rules("apchi", {}, "system", False))

    assert rules["tables"][-1] == {
        "privileges": ["SELECT", "INSERT", "UPDATE", "DELETE", "OWNERSHIP", "GRANT_SELECT"]
    }


def test_the_closed_posture_removes_only_the_catch_all() -> None:
    """There is nothing else to change: Apchi's own access comes from its own rule, which is
    what leaves it able to verify and to recover."""
    grants = {"k": GRANT}
    opened = json.loads(render_rules("apchi", grants, "system", False))["tables"]
    closed = json.loads(render_rules("apchi", grants, "system", True))["tables"]

    assert closed == opened[:-1]
    assert closed[0] == {"user": "^apchi$", "catalog": "^system$", "privileges": ["SELECT"]}
    assert closed[-1]["user"] == "^acme_finance$"


async def test_an_admin_closes_the_cluster(client: AsyncClient) -> None:
    saved = await client.put(ENFORCEMENT, json={"enforced": True})

    assert saved.status_code == 200
    assert (await client.get(ENFORCEMENT)).json() == {"enforced": True}


async def test_closing_it_stages_nothing(client: AsyncClient) -> None:
    """An Admin value reaches the Cluster at the next Apply, not by being written."""
    await client.put(ENFORCEMENT, json={"enforced": True})

    review = (await client.get("/api/v1/review")).json()

    assert review["has_changes"] is False


async def test_the_system_owned_rules_say_which_posture_is_in_force(
    client: AsyncClient,
) -> None:
    """An Operator reading "everything no grant names is allowed" on a closed Cluster would
    be reading a lie."""
    open_rules = (await client.get("/api/v1/permissions/system")).json()["rules"]
    await client.put(ENFORCEMENT, json={"enforced": True})
    closed_rules = (await client.get("/api/v1/permissions/system")).json()["rules"]

    assert open_rules[-1]["rule"] == "Everything no grant names is allowed, for everyone."
    assert closed_rules[-1]["rule"] == "An identity may reach only what it has been granted."
    assert "only an Admin can" in open_rules[-1]["why"]


async def test_applying_the_closed_posture_rewrites_the_rules(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/permissions", json=GRANT)
    await _apply(applying_client)
    assert _tables(fake_kubernetes)[-1] == {
        "privileges": ["SELECT", "INSERT", "UPDATE", "DELETE", "OWNERSHIP", "GRANT_SELECT"]
    }

    await applying_client.put(ENFORCEMENT, json={"enforced": True})
    record = await _apply(applying_client, "/api/v1/admin/applies")

    assert record["stage"] == "succeeded", record
    assert record["snapshot"] is None, "a posture is not Operator configuration"
    tables = _tables(fake_kubernetes)
    assert tables[-1]["user"] == "^acme_finance$", "the catch-all is gone"
    assert tables[0]["user"] == "^apchi$", "and Apchi still has its own"


async def test_closing_the_cluster_costs_no_restart(
    applying_client: AsyncClient,
) -> None:
    """Trino re-reads the rules on its own timer, so the posture changes without a query
    dying for it."""
    await applying_client.put(ENFORCEMENT, json={"enforced": True})

    review = (await applying_client.get("/api/v1/review")).json()

    assert review["cost"]["restarts_coordinator"] is False


async def test_an_operator_cannot_change_the_posture(client: AsyncClient) -> None:
    """It is on the Admin surface, and Operators never see those paths."""
    refused = await client.put("/api/v1/permissions/system", json={"rules": []})

    assert refused.status_code == 409

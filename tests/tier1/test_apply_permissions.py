"""Applying grants.

The first Section whose Apply costs no queries: Trino re-reads the rules on its own timer,
so nothing about the pod changes and no Rollout is needed.
"""

import asyncio
import json

from httpx import AsyncClient

from app.pipeline.applies import TERMINAL
from app.sections.permissions.generator import RULES_KEY
from tests.conftest import FakeKubernetes

SECRET = "trino-access-control"
GRANT = {
    "identity": "acme_finance",
    "catalog": "iceberg",
    "schema": "finance",
    "privileges": ["SELECT"],
}
OTHER = {"identity": "etl", "catalog": "staging", "privileges": ["INSERT"]}
CATALOG = {"name": "scratch", "connector": "memory", "properties": {}}


async def _apply(client: AsyncClient, timeout: float = 180.0) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError("Apply never settled")


def _rules(kubernetes: FakeKubernetes) -> dict:
    return json.loads(kubernetes.secrets[SECRET][RULES_KEY])


async def test_applying_a_grant_delivers_it_and_restarts_nothing(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/permissions", json=GRANT)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    granted = [
        rule for rule in _rules(fake_kubernetes)["tables"] if rule.get("user") == "^acme_finance$"
    ]
    assert granted and granted[0]["schema"] == "^finance$"
    assert fake_kubernetes.restarts == [], "a permission change costs no queries"


async def test_applying_a_grant_commits_a_snapshot(applying_client: AsyncClient) -> None:
    await applying_client.post("/api/v1/permissions", json=GRANT)

    record = await _apply(applying_client)

    assert record["snapshot"] == 1
    snapshot = (await applying_client.get("/api/v1/snapshots/1")).json()
    assert list(snapshot["sections"]["permissions"]) == ["acme_finance:iceberg:finance:*"]


async def test_removing_a_grant_removes_it_from_the_file(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/permissions", json=GRANT)
    await _apply(applying_client)
    await applying_client.delete("/api/v1/permissions/acme_finance:iceberg:finance:*")

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    assert not [
        rule for rule in _rules(fake_kubernetes)["tables"] if rule.get("user") == "^acme_finance$"
    ]


async def test_reverting_the_grants_leaves_the_catalogs_alone(
    applying_client: AsyncClient,
) -> None:
    await applying_client.post("/api/v1/catalogs", json=CATALOG)
    await applying_client.post("/api/v1/permissions", json=GRANT)
    await _apply(applying_client)
    await applying_client.post("/api/v1/permissions", json=OTHER)
    await _apply(applying_client)

    effect = (await applying_client.post("/api/v1/permissions/revert", json={"snapshot": 1})).json()

    assert effect["sections"] == ["permissions"]
    assert effect["cost"]["restarts_coordinator"] is False
    assert [g["key"] for g in (await applying_client.get("/api/v1/permissions")).json()] == [
        "acme_finance:iceberg:finance:*"
    ]
    assert [c["name"] for c in (await applying_client.get("/api/v1/catalogs")).json()] == [
        "scratch"
    ]


async def test_a_grant_that_changes_nothing_else_restarts_nothing(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """A Candidate with a rollout-required Section untouched must not restart for a
    permission change."""
    await applying_client.post(
        "/api/v1/event-listeners",
        json={
            "name": "audit",
            "type": "http",
            "properties": {"http-event-listener.connect-ingest-uri": "http://c:8080/e"},
        },
    )
    await _apply(applying_client)
    restarts = len(fake_kubernetes.restarts)

    await applying_client.post("/api/v1/permissions", json=GRANT)
    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    assert len(fake_kubernetes.restarts) == restarts, "the listener did not change"


async def test_the_probe_is_given_the_rules_and_told_to_read_them(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """A file Trino was not told to read is a file Trino never rejects, so holding the rules
    without the properties pointing at them would prove nothing.

    What this catches is the failure a running Cluster hides: Trino keeps the old rules when
    a refresh fails, so a malformed file changes nothing today and stops the coordinator
    starting whenever it next restarts.
    """
    await applying_client.post("/api/v1/permissions", json=GRANT)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    delivered = fake_kubernetes.secret_history[fake_kubernetes.pod_history[-1]]
    assert "^acme_finance$" in delivered[RULES_KEY]
    assert (
        "security.config-file=/etc/trino/access-control/rules.json"
        in (delivered["access-control.properties"])
    )


async def test_a_candidate_with_no_grants_still_needs_no_pod(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The file is then entirely generated and constant, so there is nothing a coordinator
    could reject that has not been rejected before."""
    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    assert fake_kubernetes.pod_history == []

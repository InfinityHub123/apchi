"""Section Revert and Full Rollback for a Section Trino adopts only by restarting.

Both still only stage. What changes is what they have to tell an Operator: applying this
recovery will restart the coordinator and destroy every running and queued query. A recovery
action that costs that much must not be quieter about it than the change that made it
necessary.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.pipeline.applies import TERMINAL
from app.pipeline.impact import RESTART_WARNING
from app.sections.registry import SECTIONS
from tests.conftest import RESTART_GROUP, RESTART_SECTION, change_the_restart, stage_a_restart

KEPT = {"name": "kept", "connector": "memory", "properties": {}}
ADDED = {"name": "added", "connector": "memory", "properties": {}}
GROUP = f"/api/v1/resource-groups/{RESTART_GROUP}"
REVERT = "/api/v1/resource-groups/revert"


async def _apply(client: AsyncClient, timeout: float = 180.0) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            assert record["stage"] == "succeeded", record.get("failure_reason")
            return record
        await asyncio.sleep(0.05)
    raise AssertionError("Apply never settled")


@pytest.fixture
async def two_snapshots(applying_client: AsyncClient) -> AsyncClient:
    """Snapshot 1 holds `kept` and a group limited to 10; Snapshot 2 adds `added` and
    raises the limit to 20."""
    await applying_client.post("/api/v1/catalogs", json=KEPT)
    await stage_a_restart(applying_client, limit=10)
    await _apply(applying_client)

    await applying_client.post("/api/v1/catalogs", json=ADDED)
    await change_the_restart(applying_client, limit=20)
    await _apply(applying_client)
    return applying_client


async def test_reverting_the_groups_leaves_the_catalogs_alone(
    two_snapshots: AsyncClient,
) -> None:
    effect = (await two_snapshots.post(REVERT, json={"snapshot": 1})).json()

    assert effect["sections"] == [RESTART_SECTION]
    group = (await two_snapshots.get(GROUP)).json()
    assert group["hard_concurrency_limit"] == 10
    staged = [c["name"] for c in (await two_snapshots.get("/api/v1/catalogs")).json()]
    assert staged == ["added", "kept"], "the Catalogs Section was not touched"


async def test_a_group_revert_says_it_will_restart_the_coordinator(
    two_snapshots: AsyncClient,
) -> None:
    effect = (await two_snapshots.post(REVERT, json={"snapshot": 1})).json()

    assert effect["cost"]["restarts_coordinator"] is True
    assert effect["cost"]["rollout_sections"] == [RESTART_SECTION]
    assert RESTART_WARNING in effect["summary"]
    assert effect["cost"]["queries_at_risk"] == 0


async def test_a_group_revert_still_says_the_combination_has_never_run(
    two_snapshots: AsyncClient,
) -> None:
    """The restart warning must not crowd out the older one. Reverting one Section while
    the others stay put is novel configuration, not a return to a known-good state."""
    effect = (await two_snapshots.post(REVERT, json={"snapshot": 1})).json()

    assert "has never run" in effect["summary"]
    assert effect["other_sections_stay_at"] == 2


async def test_a_catalogs_revert_costs_no_restart(two_snapshots: AsyncClient) -> None:
    """Reverting a Section Trino adopts live must not warn about a restart it will not
    perform."""
    effect = (await two_snapshots.post("/api/v1/catalogs/revert", json={"snapshot": 1})).json()

    assert effect["cost"]["restarts_coordinator"] is False
    assert effect["cost"]["rollout_sections"] == []
    assert effect["catalogs_dropped"] == ["added"]
    assert RESTART_WARNING not in effect["summary"]
    assert "drop 1 catalog: added" in effect["summary"]


async def test_reverting_to_a_snapshot_with_the_same_groups_reports_no_restart(
    two_snapshots: AsyncClient,
) -> None:
    """Nothing would change, so nothing would restart."""
    effect = (await two_snapshots.post(REVERT, json={"snapshot": 2})).json()

    assert effect["cost"]["restarts_coordinator"] is False
    assert effect["cost"]["warning"] is None


async def test_a_full_rollback_restores_every_section_and_warns(
    two_snapshots: AsyncClient,
) -> None:
    effect = (await two_snapshots.post("/api/v1/candidate/rollback", json={"snapshot": 1})).json()

    # A Full Rollback replaces the whole Candidate, so it names every registered Section,
    # not only the ones this Snapshot happens to differ in.
    assert effect["sections"] == list(SECTIONS)
    assert effect["other_sections_stay_at"] is None
    assert effect["catalogs_dropped"] == ["added"]
    assert effect["cost"]["restarts_coordinator"] is True
    assert RESTART_WARNING in effect["summary"]
    group = (await two_snapshots.get(GROUP)).json()
    assert group["hard_concurrency_limit"] == 10


async def test_applying_a_group_revert_produces_a_new_snapshot(
    two_snapshots: AsyncClient, fake_kubernetes
) -> None:
    """History is never rewritten: Snapshot 1's content comes back as Snapshot 3."""
    before = (await two_snapshots.get("/api/v1/snapshots/1")).json()
    await two_snapshots.post(REVERT, json={"snapshot": 1})
    fake_kubernetes.restarts.clear()

    record = await _apply(two_snapshots)

    assert record["snapshot"] == 3
    assert record["rolled_out"] is True, "a group change reaches Trino only by restarting"
    assert len(fake_kubernetes.restarts) == 1
    after = (await two_snapshots.get("/api/v1/snapshots/1")).json()
    assert after == before
    restored = (await two_snapshots.get("/api/v1/snapshots/3")).json()
    assert restored["sections"][RESTART_SECTION] == before["sections"][RESTART_SECTION]


async def test_a_revert_is_refused_under_maintenance_mode(two_snapshots: AsyncClient) -> None:
    await two_snapshots.put("/api/v1/admin/maintenance-mode", json={"engaged": True})

    refused = await two_snapshots.post(REVERT, json={"snapshot": 1})

    assert refused.status_code == 409
    assert refused.json()["code"] == "maintenance_mode"


async def test_reverting_groups_to_an_unknown_snapshot_is_not_found(
    two_snapshots: AsyncClient,
) -> None:
    missing = await two_snapshots.post(REVERT, json={"snapshot": 99})

    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"

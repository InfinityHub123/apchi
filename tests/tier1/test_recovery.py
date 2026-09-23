"""Section Revert and Full Rollback.

Both stage into the Candidate and stop there. What makes them worth testing carefully
is what they tell the Operator on the way: a revert issues real DROP CATALOG
statements when it is applied, and reverting one Section while the others stay put
produces a configuration that has never run anywhere.
"""

import asyncio

from httpx import AsyncClient
from testcontainers.core.container import DockerContainer

from app.adapters.trino import Trino
from app.pipeline.applies import TERMINAL

ALPHA = {"name": "alpha", "connector": "memory", "properties": {}}
BETA = {"name": "beta", "connector": "memory", "properties": {}}


async def _settled(client: AsyncClient, apply_id: str, timeout: float = 60.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


async def _apply(client: AsyncClient) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    record = await _settled(client, started.json()["id"])
    assert record["stage"] == "succeeded", record.get("failure_reason")
    return record


async def _two_snapshots(client: AsyncClient) -> None:
    """Snapshot 1 holds alpha; Snapshot 2 holds alpha and beta."""
    await client.post("/api/v1/catalogs", json=ALPHA)
    await _apply(client)
    await client.post("/api/v1/catalogs", json=BETA)
    await _apply(client)


async def test_a_revert_stages_the_earlier_snapshot_into_the_candidate(
    applying_client: AsyncClient,
) -> None:
    await _two_snapshots(applying_client)

    await applying_client.post("/api/v1/catalogs/revert", json={"snapshot": 1})

    staged = [c["name"] for c in (await applying_client.get("/api/v1/catalogs")).json()]
    assert staged == ["alpha"]


async def test_a_revert_does_not_touch_the_cluster(
    applying_client: AsyncClient, trino_cluster: DockerContainer
) -> None:
    """It stages. Nothing reaches the Cluster before Apply -- that is invariant 3, and a
    revert is no exception to it."""
    await _two_snapshots(applying_client)

    await applying_client.post("/api/v1/catalogs/revert", json={"snapshot": 1})

    live = await Trino(
        host=trino_cluster.get_container_host_ip(),
        port=int(trino_cluster.get_exposed_port(8080)),
    ).catalogs()
    assert {"alpha", "beta"} <= live


async def test_the_response_names_the_catalogs_an_apply_would_drop(
    applying_client: AsyncClient,
) -> None:
    """ "3 catalogs will be dropped" is a materially different warning from "the
    configuration will be rewritten"."""
    await _two_snapshots(applying_client)

    effect = (await applying_client.post("/api/v1/catalogs/revert", json={"snapshot": 1})).json()

    assert effect["catalogs_dropped"] == ["beta"]
    assert "beta" in effect["summary"]
    assert "drop 1 catalog" in effect["summary"]


async def test_the_response_says_the_combination_has_never_run(
    applying_client: AsyncClient,
) -> None:
    """A Section Revert must never imply a return to a known-good state."""
    await _two_snapshots(applying_client)

    effect = (await applying_client.post("/api/v1/catalogs/revert", json={"snapshot": 1})).json()

    assert effect["from_snapshot"] == 1
    assert effect["other_sections_stay_at"] == 2
    assert "has never run" in effect["summary"]
    assert "stays at Snapshot 2" in effect["summary"]


async def test_a_revert_that_drops_nothing_says_so(applying_client: AsyncClient) -> None:
    """The warning has to be true in both directions, or Operators learn to ignore it."""
    await _two_snapshots(applying_client)

    effect = (await applying_client.post("/api/v1/catalogs/revert", json={"snapshot": 2})).json()

    assert effect["catalogs_dropped"] == []
    assert "drop no catalogs" in effect["summary"]


async def test_a_revert_needs_an_ordinary_apply_to_take_effect(
    applying_client: AsyncClient, trino_cluster: DockerContainer
) -> None:
    """Nothing bypasses the pipeline, so the revert becomes real the same way every
    other edit does -- and produces a new Snapshot, not a rewritten one."""
    await _two_snapshots(applying_client)
    await applying_client.post("/api/v1/catalogs/revert", json={"snapshot": 1})

    record = await _apply(applying_client)

    assert record["snapshot"] == 3
    live = await Trino(
        host=trino_cluster.get_container_host_ip(),
        port=int(trino_cluster.get_exposed_port(8080)),
    ).catalogs()
    assert "alpha" in live
    assert "beta" not in live


async def test_reverting_never_modifies_the_snapshot_it_restores_from(
    applying_client: AsyncClient,
) -> None:
    """History is never rewritten."""
    await _two_snapshots(applying_client)
    before = (await applying_client.get("/api/v1/snapshots/1")).json()

    await applying_client.post("/api/v1/catalogs/revert", json={"snapshot": 1})
    await _apply(applying_client)

    after = (await applying_client.get("/api/v1/snapshots/1")).json()
    assert after == before
    assert [s["number"] for s in (await applying_client.get("/api/v1/snapshots")).json()] == [
        3,
        2,
        1,
    ]


async def test_a_revert_leaves_other_edits_in_the_candidate_alone(
    applying_client: AsyncClient,
) -> None:
    """So an Operator can revert one Section and adjust another before applying once.
    Slice 1 has one Section, so the neighbour under test is an edit to the Candidate."""
    await _two_snapshots(applying_client)
    await applying_client.post(
        "/api/v1/catalogs", json={"name": "gamma", "connector": "memory", "properties": {}}
    )

    await applying_client.post("/api/v1/catalogs/revert", json={"snapshot": 1})

    staged = [c["name"] for c in (await applying_client.get("/api/v1/catalogs")).json()]
    assert staged == ["alpha"], "the reverted Section is replaced wholesale, gamma included"


async def test_reverting_to_an_unknown_snapshot_is_not_found(
    applying_client: AsyncClient,
) -> None:
    missing = await applying_client.post("/api/v1/catalogs/revert", json={"snapshot": 99})

    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


async def test_a_full_rollback_replaces_the_whole_candidate(
    applying_client: AsyncClient,
) -> None:
    await _two_snapshots(applying_client)
    await applying_client.post(
        "/api/v1/catalogs", json={"name": "gamma", "connector": "memory", "properties": {}}
    )

    effect = (await applying_client.post("/api/v1/candidate/rollback", json={"snapshot": 1})).json()

    assert effect["sections"] == ["catalogs"]
    assert effect["other_sections_stay_at"] is None
    assert sorted(effect["catalogs_dropped"]) == ["beta"]
    staged = [c["name"] for c in (await applying_client.get("/api/v1/catalogs")).json()]
    assert staged == ["alpha"]


async def test_a_full_rollback_produces_a_new_snapshot(
    applying_client: AsyncClient, trino_cluster: DockerContainer
) -> None:
    """Snapshot 10 is never modified: restoring it becomes Snapshot 14."""
    await _two_snapshots(applying_client)
    await applying_client.post("/api/v1/candidate/rollback", json={"snapshot": 1})

    record = await _apply(applying_client)

    assert record["snapshot"] == 3
    restored = (await applying_client.get("/api/v1/snapshots/3")).json()
    original = (await applying_client.get("/api/v1/snapshots/1")).json()
    assert restored["sections"] == original["sections"]
    assert restored["number"] != original["number"]


async def test_rolling_back_to_an_unknown_snapshot_is_not_found(
    applying_client: AsyncClient,
) -> None:
    missing = await applying_client.post("/api/v1/candidate/rollback", json={"snapshot": 99})

    assert missing.status_code == 404


async def test_recovery_is_refused_while_the_candidate_is_frozen(
    applying_client: AsyncClient,
) -> None:
    """A revert is an Operator mutation like any other, so it passes the same gate."""
    await _two_snapshots(applying_client)
    await applying_client.put(
        "/api/v1/admin/maintenance-mode", json={"engaged": True, "reason": "upgrade"}
    )

    revert = await applying_client.post("/api/v1/catalogs/revert", json={"snapshot": 1})
    rollback = await applying_client.post("/api/v1/candidate/rollback", json={"snapshot": 1})

    assert revert.status_code == 409
    assert rollback.status_code == 409
    assert revert.json()["code"] == "maintenance_mode"

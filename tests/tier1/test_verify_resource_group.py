"""Verification proving the resource group configuration is in force.

The first functional check a file-based Section has been able to have: Trino records the
group a query ran in, so the smoke query Verification already runs is evidence.

What makes tier 1 able to test this at all is a quirk of the seam. The Cluster here is a
real Trino with no resource group manager configured, and such a Trino still files every
query under a group: its legacy manager calls that group `global`. So a Candidate whose
selectors point at `global` agrees with what this Cluster reports, and one pointing anywhere
else disagrees -- which is precisely the failure this check exists to catch, available here
without having to break a cluster to produce it.
"""

import asyncio

from httpx import AsyncClient

from app.pipeline.applies import TERMINAL

GLOBAL = {"path": "global", "hard_concurrency_limit": 100}
ANALYTICS = {"path": "analytics", "hard_concurrency_limit": 10}


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


async def test_a_query_landing_where_the_selectors_say_passes(applying_client: AsyncClient) -> None:
    await applying_client.post("/api/v1/resource-groups", json=GLOBAL)
    await applying_client.put(
        "/api/v1/resource-groups/selectors", json={"selectors": [{"group": "global"}]}
    )

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record


async def test_a_query_landing_elsewhere_fails_verification_naming_both_groups(
    applying_client: AsyncClient,
) -> None:
    """What a coordinator running the old file looks like: the configuration says one thing
    and the Cluster does another."""
    await applying_client.post("/api/v1/resource-groups", json=ANALYTICS)
    await applying_client.put(
        "/api/v1/resource-groups/selectors", json={"selectors": [{"group": "analytics"}]}
    )

    record = await _apply(applying_client)

    assert record["stage"] == "failed"
    assert "'analytics'" in record["failure_reason"], record["failure_reason"]
    assert "'global'" in record["failure_reason"]
    assert record["rollback"] == "succeeded", "a Verification failure is rolled back like any other"


async def test_a_candidate_with_no_selectors_is_not_judged(applying_client: AsyncClient) -> None:
    """Nothing claims where anything goes, so there is nothing for the Cluster to
    contradict."""
    await applying_client.post("/api/v1/resource-groups", json=ANALYTICS)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record


async def test_a_cluster_with_no_resource_groups_at_all_is_unaffected(
    applying_client: AsyncClient,
) -> None:
    """The check must not invent a failure for a Section nobody has configured."""
    await applying_client.post(
        "/api/v1/catalogs", json={"name": "scratch", "connector": "memory", "properties": {}}
    )

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record


async def test_a_selector_apchi_cannot_evaluate_is_not_judged(
    applying_client: AsyncClient,
) -> None:
    """Apchi does not know which groups its own user belongs to, and a guess here would fail
    Verification on a healthy Cluster and roll back a good Apply."""
    await applying_client.post("/api/v1/resource-groups", json=ANALYTICS)
    await applying_client.put(
        "/api/v1/resource-groups/selectors",
        json={"selectors": [{"user_group": "platform", "group": "analytics"}]},
    )

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record


async def test_a_selector_that_does_not_match_apchis_query_is_not_judged(
    applying_client: AsyncClient,
) -> None:
    """No selector claims this query, so where Trino puts it instead is Trino's business."""
    await applying_client.post("/api/v1/resource-groups", json=ANALYTICS)
    await applying_client.put(
        "/api/v1/resource-groups/selectors",
        json={"selectors": [{"user": "someone_else", "group": "analytics"}]},
    )

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record


async def test_the_selector_match_is_a_full_match_like_trinos(
    applying_client: AsyncClient,
) -> None:
    """Verified against a running coordinator: a selector for user `apch` does not match
    `apchi`. Predicting with a partial match would expect a group Trino never chose."""
    await applying_client.post("/api/v1/resource-groups", json=ANALYTICS)
    await applying_client.put(
        "/api/v1/resource-groups/selectors",
        json={"selectors": [{"user": "apch", "group": "analytics"}]},
    )

    record = await _apply(applying_client)

    # A partial match would have expected `analytics` and failed against the `global` the
    # Cluster reports.
    assert record["stage"] == "succeeded", record

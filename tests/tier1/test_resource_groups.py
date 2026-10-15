"""Resource Groups in the Configuration Candidate.

Staging only. The hierarchy is the interesting part: groups are addressed by a dotted path,
so the shape of the tree is expressed in the keys rather than in nesting, and the rules that
keep it a tree are the ones worth testing.
"""

from httpx import AsyncClient

GLOBAL = {"path": "global", "hard_concurrency_limit": 100, "max_queued": 1000}
ETL = {"path": "global.etl", "hard_concurrency_limit": 10, "max_queued": 100}
ADHOC = {"path": "global.adhoc", "hard_concurrency_limit": 20, "soft_memory_limit": "30%"}


async def _tree(client: AsyncClient) -> None:
    for group in (GLOBAL, ETL, ADHOC):
        created = await client.post("/api/v1/resource-groups", json=group)
        assert created.status_code == 201, created.json()


async def test_a_staged_group_is_listed(client: AsyncClient) -> None:
    created = await client.post("/api/v1/resource-groups", json=GLOBAL)

    assert created.status_code == 201
    assert created.json()["path"] == "global"
    listed = (await client.get("/api/v1/resource-groups")).json()
    assert [group["path"] for group in listed] == ["global"]


async def test_only_what_trino_requires_is_required(client: AsyncClient) -> None:
    """A group without maxQueued starts on a real coordinator, so Apchi does not refuse it.
    Without hardConcurrencyLimit it does not, so Apchi does."""
    accepted = await client.post(
        "/api/v1/resource-groups", json={"path": "global", "hard_concurrency_limit": 1}
    )
    refused = await client.post("/api/v1/resource-groups", json={"path": "other"})

    assert accepted.status_code == 201
    assert refused.status_code == 422
    assert "hard_concurrency_limit" in str(refused.json()["details"])


async def test_a_group_is_added_under_a_parent(client: AsyncClient) -> None:
    await client.post("/api/v1/resource-groups", json=GLOBAL)

    created = await client.post("/api/v1/resource-groups", json=ETL)

    assert created.status_code == 201
    assert (await client.get("/api/v1/resource-groups/global.etl")).json()["max_queued"] == 100


async def test_a_group_under_a_parent_that_does_not_exist_is_refused(client: AsyncClient) -> None:
    """The tree is rebuilt from the roots down, so an orphan would render as nothing at
    all -- staged, and silently absent from the file."""
    refused = await client.post("/api/v1/resource-groups", json=ETL)

    assert refused.status_code == 422
    assert "global" in refused.json()["message"]


async def test_a_group_name_may_not_contain_a_dot(client: AsyncClient) -> None:
    """Trino rejects one outright -- "Invalid resource group name. 'a.b' contains a '.'" --
    which is what makes a dotted path unambiguous."""
    refused = await client.post(
        "/api/v1/resource-groups", json={"path": "global.", "hard_concurrency_limit": 1}
    )

    assert refused.status_code == 422


async def test_a_group_is_edited_in_place(client: AsyncClient) -> None:
    await client.post("/api/v1/resource-groups", json=GLOBAL)

    updated = await client.put(
        "/api/v1/resource-groups/global", json={"hard_concurrency_limit": 50, "max_queued": 500}
    )

    assert updated.status_code == 200
    assert updated.json()["hard_concurrency_limit"] == 50


async def test_an_unknown_field_is_refused_with_its_name(client: AsyncClient) -> None:
    """Trino owns this format and documents it, so there is no pass-through: what Apchi
    does not model, it refuses."""
    refused = await client.post(
        "/api/v1/resource-groups",
        json={"path": "global", "hard_concurrency_limit": 1, "nonsense": 2},
    )

    assert refused.status_code == 422
    assert "nonsense" in str(refused.json()["details"])


async def test_an_unknown_scheduling_policy_is_refused(client: AsyncClient) -> None:
    refused = await client.post(
        "/api/v1/resource-groups",
        json={"path": "global", "hard_concurrency_limit": 1, "scheduling_policy": "round_robin"},
    )

    assert refused.status_code == 422


async def test_a_duplicate_group_is_a_conflict(client: AsyncClient) -> None:
    await client.post("/api/v1/resource-groups", json=GLOBAL)

    refused = await client.post("/api/v1/resource-groups", json=GLOBAL)

    assert refused.status_code == 409


async def test_removing_a_group_with_subgroups_is_refused_naming_them(
    client: AsyncClient,
) -> None:
    """Deleting a parent would take its whole subtree with it, silently."""
    await _tree(client)

    refused = await client.delete("/api/v1/resource-groups/global")

    assert refused.status_code == 409
    assert "global.etl" in refused.json()["message"]
    assert (await client.get("/api/v1/resource-groups/global")).status_code == 200


async def test_a_leaf_is_removed(client: AsyncClient) -> None:
    await _tree(client)

    removed = await client.delete("/api/v1/resource-groups/global.etl")

    assert removed.status_code == 204
    assert [g["path"] for g in (await client.get("/api/v1/resource-groups")).json()] == [
        "global",
        "global.adhoc",
    ]


async def test_an_unknown_group_is_not_found(client: AsyncClient) -> None:
    missing = await client.get("/api/v1/resource-groups/nope")

    assert missing.status_code == 404


async def test_selectors_are_read_and_replaced_as_one_ordered_list(client: AsyncClient) -> None:
    await _tree(client)

    saved = await client.put(
        "/api/v1/resource-groups/selectors",
        json={
            "selectors": [
                {"user": "etl_.*", "group": "global.etl"},
                {"group": "global.adhoc"},
            ]
        },
    )

    assert saved.status_code == 200
    assert [s["group"] for s in saved.json()["selectors"]] == ["global.etl", "global.adhoc"]
    listed = (await client.get("/api/v1/resource-groups/selectors")).json()
    assert [s["group"] for s in listed["selectors"]] == ["global.etl", "global.adhoc"]


async def test_a_selector_may_name_a_group_that_is_not_staged_yet(client: AsyncClient) -> None:
    """Refused at request time, an Operator could not write a selector before the group it
    points at. Validate is where the whole Candidate is judged."""
    accepted = await client.put(
        "/api/v1/resource-groups/selectors", json={"selectors": [{"group": "later"}]}
    )

    assert accepted.status_code == 200


async def test_an_unknown_selector_field_is_refused(client: AsyncClient) -> None:
    refused = await client.put(
        "/api/v1/resource-groups/selectors",
        json={"selectors": [{"group": "global", "resource_estimate": {"executionTime": "1h"}}]},
    )

    assert refused.status_code == 422


async def test_a_group_may_not_be_called_selectors_in_a_way_that_collides(
    client: AsyncClient,
) -> None:
    """`selectors` is a legal Trino group name, so it must stay legal here -- the list lives
    under a key no group can spell."""
    created = await client.post(
        "/api/v1/resource-groups", json={"path": "selectors", "hard_concurrency_limit": 1}
    )
    await client.put(
        "/api/v1/resource-groups/selectors", json={"selectors": [{"group": "selectors"}]}
    )

    assert created.status_code == 201
    assert [g["path"] for g in (await client.get("/api/v1/resource-groups")).json()] == [
        "selectors"
    ]
    assert (await client.get("/api/v1/resource-groups/selectors")).json()["selectors"] != []


async def test_groups_appear_in_review_by_path_and_cost_a_restart(client: AsyncClient) -> None:
    await _tree(client)

    review = (await client.get("/api/v1/review")).json()

    groups = next(s for s in review["sections"] if s["section"] == "resource_groups")
    assert [(c["resource"], c["change"]) for c in groups["changes"]] == [
        ("global", "added"),
        ("global.adhoc", "added"),
        ("global.etl", "added"),
    ]
    assert review["cost"]["restarts_coordinator"] is True


async def test_reset_discards_staged_groups(client: AsyncClient) -> None:
    await _tree(client)

    await client.post("/api/v1/candidate/reset")

    assert (await client.get("/api/v1/resource-groups")).json() == []


async def test_group_mutations_are_refused_under_maintenance_mode(client: AsyncClient) -> None:
    await client.put(
        "/api/v1/admin/maintenance-mode", json={"engaged": True, "reason": "Trino upgrade"}
    )

    refused = await client.post("/api/v1/resource-groups", json=GLOBAL)

    assert refused.status_code == 409


async def test_a_soft_cpu_limit_without_a_hard_one_is_refused(client: AsyncClient) -> None:
    """Trino will not start with one and not the other: "Must specify hard CPU limit in
    addition to soft limit". Within one group, so it is caught at request time."""
    refused = await client.post(
        "/api/v1/resource-groups",
        json={"path": "global", "hard_concurrency_limit": 1, "soft_cpu_limit": "30m"},
    )

    assert refused.status_code == 422
    assert "hard_cpu_limit" in str(refused.json()["details"])


async def test_the_cpu_quota_period_is_read_and_replaced(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/resource-groups/settings")).json() == {
        "cpu_quota_period": None
    }

    saved = await client.put("/api/v1/resource-groups/settings", json={"cpu_quota_period": "1h"})

    assert saved.status_code == 200
    assert (await client.get("/api/v1/resource-groups/settings")).json()["cpu_quota_period"] == "1h"

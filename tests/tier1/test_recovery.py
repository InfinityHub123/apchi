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
from app.sections.registry import SECTIONS
from tests.conftest import container_url

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

    live = await Trino(url=container_url(trino_cluster)).catalogs()
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
    live = await Trino(url=container_url(trino_cluster)).catalogs()
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
    assert [
        s["number"] for s in (await applying_client.get("/api/v1/snapshots")).json()["items"]
    ] == [
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

    assert effect["sections"] == list(SECTIONS), "a Full Rollback replaces every Section"
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


# --- dry run -------------------------------------------------------------------------------


async def test_a_dry_run_reports_the_same_effect_and_stages_nothing(
    applying_client: AsyncClient,
) -> None:
    """The question the endpoint could not answer before: *what would this do*.

    The effect is identical to the real call's, because it was always computed from the
    Candidate in memory -- what the real call adds is the save. Only `staged` differs.
    """
    await _two_snapshots(applying_client)

    preview = (
        await applying_client.post("/api/v1/catalogs/revert", json={"snapshot": 1, "dry_run": True})
    ).json()

    assert preview["staged"] is False
    assert sorted(preview["catalogs_dropped"]) == ["beta"]
    staged = [c["name"] for c in (await applying_client.get("/api/v1/catalogs")).json()]
    assert staged == ["alpha", "beta"], "the Candidate is untouched"

    real = (await applying_client.post("/api/v1/catalogs/revert", json={"snapshot": 1})).json()

    assert real["staged"] is True
    assert {k: v for k, v in real.items() if k != "staged"} == {
        k: v for k, v in preview.items() if k != "staged"
    }, "the preview was the answer, not an approximation of it"


async def test_a_dry_run_of_a_full_rollback_does_not_discard_anyones_work(
    applying_client: AsyncClient,
) -> None:
    """Where this matters most. A Full Rollback discards every staged change in all six
    Sections, including other people's, and before this the way to see what that amounted to
    was to cause it."""
    await _two_snapshots(applying_client)
    await applying_client.post(
        "/api/v1/catalogs", json={"name": "gamma", "connector": "memory", "properties": {}}
    )

    preview = (
        await applying_client.post(
            "/api/v1/candidate/rollback", json={"snapshot": 1, "dry_run": True}
        )
    ).json()

    assert preview["staged"] is False
    assert preview["sections"] == list(SECTIONS)
    assert sorted(preview["catalogs_dropped"]) == ["beta"]
    staged = [c["name"] for c in (await applying_client.get("/api/v1/catalogs")).json()]
    assert staged == ["alpha", "beta", "gamma"], "gamma survives a preview"


async def test_a_dry_run_records_nothing_in_the_audit(applying_client: AsyncClient) -> None:
    """It changed nothing, so there is nothing to attribute. The audit is written by
    `candidates.save`, which is the one call a dry run skips -- and an audit full of reverts
    nobody performed is worse than one that omits a question somebody asked."""
    await _two_snapshots(applying_client)
    before = (await applying_client.get("/api/v1/audit", params={"action": "reverted"})).json()

    await applying_client.post("/api/v1/catalogs/revert", json={"snapshot": 1, "dry_run": True})

    after = (await applying_client.get("/api/v1/audit", params={"action": "reverted"})).json()
    assert len(after["items"]) == len(before["items"])

    await applying_client.post("/api/v1/catalogs/revert", json={"snapshot": 1})

    recorded = (await applying_client.get("/api/v1/audit", params={"action": "reverted"})).json()
    assert len(recorded["items"]) == len(before["items"]) + 1


async def test_a_mistyped_dry_run_is_refused_rather_than_obeyed(
    applying_client: AsyncClient,
) -> None:
    """Why the flag is in the body rather than a query parameter, and the whole reason that
    choice is worth making.

    `RevertRequest` forbids unknown fields, so `dryrun` is a 422 and the Candidate is
    untouched. A mistyped *query* parameter would be ignored, and the request somebody meant
    to make harmless would replace the Section for real.
    """
    await _two_snapshots(applying_client)

    refused = await applying_client.post(
        "/api/v1/catalogs/revert", json={"snapshot": 1, "dryrun": True}
    )

    assert refused.status_code == 422
    staged = [c["name"] for c in (await applying_client.get("/api/v1/catalogs")).json()]
    assert staged == ["alpha", "beta"], "nothing was reverted by the refused request"

    ignored = await applying_client.post(
        "/api/v1/catalogs/revert?dry_run=true", json={"snapshot": 1}
    )

    assert ignored.status_code == 200
    assert ignored.json()["staged"] is True, (
        "a query parameter is not the flag; this one staged, which is what the body prevents"
    )


async def test_a_dry_run_to_an_unknown_snapshot_is_still_not_found(
    applying_client: AsyncClient,
) -> None:
    """A preview of an impossible revert is an error, not an empty effect."""
    await _two_snapshots(applying_client)

    response = await applying_client.post(
        "/api/v1/catalogs/revert", json={"snapshot": 99, "dry_run": True}
    )

    assert response.status_code == 404


async def test_every_revert_endpoint_offers_the_dry_run(applying_client: AsyncClient) -> None:
    """One request model shared by seven routes, so the flag cannot reach some of them and
    not others. Asserted against the live app rather than by reading the source."""
    await _two_snapshots(applying_client)

    for path in (
        "/api/v1/catalogs/revert",
        "/api/v1/certificates/revert",
        "/api/v1/certificate-mapping/revert",
        "/api/v1/permissions/revert",
        "/api/v1/resource-groups/revert",
        "/api/v1/event-listeners/revert",
        "/api/v1/candidate/rollback",
    ):
        response = await applying_client.post(path, json={"snapshot": 1, "dry_run": True})

        assert response.status_code == 200, f"{path}: {response.text}"
        assert response.json()["staged"] is False, path

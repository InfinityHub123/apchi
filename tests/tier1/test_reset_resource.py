"""Resetting one staged change and leaving every other one.

The Candidate is shared, so the unit of undo matters more than it would in a private draft.
Before this the smallest one was a Section Revert, which discards everyone's edits to that
Section along with the one being undone -- and the frontend was emulating a per-resource
undo by re-sending the Snapshot's version, which is audited as an ordinary edit and cannot
work for a certificate at all (#218, item 13).
"""

import asyncio

from httpx import AsyncClient

from app.pipeline.applies import TERMINAL
from tests.certificates import bundle

RESET = "/api/v1/candidate/reset-resource"
PG = {
    "name": "finance",
    "connector": "postgresql",
    "properties": {"connection-url": "jdbc:postgresql://db:5432/f", "connection-user": "t"},
}
LEDGER = {**PG, "name": "ledger"}
MEMORY = {"name": "scratch", "connector": "memory", "properties": {}}


async def _applied(client: AsyncClient) -> None:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    for _ in range(1200):
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            assert record["stage"] == "succeeded", record.get("failure_reason")
            return
        await asyncio.sleep(0.05)
    raise AssertionError("Apply never settled")


async def _staged(client: AsyncClient) -> list[tuple[str, str, str]]:
    review = (await client.get("/api/v1/review")).json()
    return [
        (section["section"], change["resource"], change["change"])
        for section in review["sections"]
        for change in section["changes"]
    ]


async def test_resetting_an_addition_removes_it_and_nothing_else(client: AsyncClient) -> None:
    """The point of the endpoint: two people staged two catalogs, one of them is undone, and
    the other survives. A Section Revert would have taken both."""
    await client.post("/api/v1/catalogs", json=PG)
    await client.post("/api/v1/catalogs", json=LEDGER)

    response = await client.post(RESET, json={"section": "catalogs", "resource": "finance"})

    assert response.status_code == 204, response.text
    assert await _staged(client) == [("catalogs", "ledger", "added")]


async def test_resetting_an_edit_restores_the_snapshots_version(
    applying_client: AsyncClient,
) -> None:
    await applying_client.post("/api/v1/catalogs", json=MEMORY)
    await _applied(applying_client)
    patched = await applying_client.patch(
        "/api/v1/catalogs/scratch", json={"properties": {"memory.max-data-per-node": "64MB"}}
    )
    assert patched.status_code == 200, patched.text
    assert await _staged(applying_client) == [("catalogs", "scratch", "changed")]

    await applying_client.post(RESET, json={"section": "catalogs", "resource": "scratch"})

    assert await _staged(applying_client) == []
    restored = (await applying_client.get("/api/v1/catalogs/scratch")).json()
    assert restored["properties"] == {}


async def test_resetting_a_removal_brings_the_resource_back(applying_client: AsyncClient) -> None:
    await applying_client.post("/api/v1/catalogs", json=MEMORY)
    await _applied(applying_client)
    await applying_client.delete("/api/v1/catalogs/scratch")
    assert await _staged(applying_client) == [("catalogs", "scratch", "removed")]

    await applying_client.post(RESET, json={"section": "catalogs", "resource": "scratch"})

    assert await _staged(applying_client) == []
    assert (await applying_client.get("/api/v1/catalogs/scratch")).status_code == 200


async def test_a_certificate_can_be_reset(client: AsyncClient) -> None:
    """What the frontend's emulation could not do: re-sending a Snapshot's version from
    outside means uploading a ZIP, and the Snapshot's copy of the key never leaves Apchi."""
    uploaded = await client.post(
        "/api/v1/certificates",
        data={"name": "finance"},
        files={"archive": ("finance.zip", bundle(), "application/zip")},
    )
    assert uploaded.status_code == 201, uploaded.text

    await client.post(RESET, json={"section": "client_certificates", "resource": "finance"})

    assert (await client.get("/api/v1/certificates")).json() == []


async def test_a_resource_with_nothing_staged_is_not_found(client: AsyncClient) -> None:
    """Not a silent success, which would read as "undone" to somebody who typed the wrong
    name -- and the Candidate is untouched by the refusal."""
    await client.post("/api/v1/catalogs", json=PG)

    response = await client.post(RESET, json={"section": "catalogs", "resource": "finanse"})

    assert response.status_code == 404
    assert "finanse" in response.json()["message"]
    assert await _staged(client) == [("catalogs", "finance", "added")]


async def test_a_name_with_a_hash_arrives_whole(client: AsyncClient) -> None:
    """Why the resource is in the body. Resource Groups keys its selector list as
    `#selectors`; in a URL path everything from `#` on is the fragment and never reaches the
    server. The refusal quoting the full name back is the proof it arrived intact."""
    response = await client.post(
        RESET, json={"section": "resource_groups", "resource": "#selectors"}
    )

    assert response.status_code == 404
    assert "'#selectors'" in response.json()["message"]


async def test_a_mistyped_field_is_refused_rather_than_guessed(client: AsyncClient) -> None:
    await client.post("/api/v1/catalogs", json=PG)

    response = await client.post(RESET, json={"section": "catalogs", "name": "finance"})

    assert response.status_code == 422
    assert await _staged(client) == [("catalogs", "finance", "added")]


async def test_the_reset_is_audited_with_the_resource_named(client: AsyncClient) -> None:
    """One `reset` naming the resource, rather than a derived `removed` that would read as
    somebody deleting the catalog -- and the resource is what distinguishes it from a Reset
    of the whole Candidate, which carries none."""
    await client.post("/api/v1/catalogs", json=PG)

    await client.post(RESET, json={"section": "catalogs", "resource": "finance"})

    [event] = (await client.get("/api/v1/audit", params={"action": "reset"})).json()["items"]
    assert event["category"] == "catalogs"
    assert event["resource"] == "finance"
    assert event["detail"] == "restored from an empty baseline"


async def test_it_is_refused_while_maintenance_mode_is_engaged(client: AsyncClient) -> None:
    """It changes the Candidate, so it carries the gate every Operator edit carries."""
    await client.post("/api/v1/catalogs", json=PG)
    await client.put(
        "/api/v1/admin/maintenance-mode", json={"engaged": True, "reason": "Upgrading."}
    )

    response = await client.post(RESET, json={"section": "catalogs", "resource": "finance"})

    assert response.status_code == 409
    assert response.json()["code"] == "maintenance_mode"
    assert await _staged(client) == [("catalogs", "finance", "added")]

"""Exporting a configuration, at both scopes.

An Operator has a configuration they like and wants it somewhere else, or wants somebody
else's here to edit. These tests are about what a document says: that it is one of these
documents, that it carries the Sections whole, that the Admin scope and only the Admin scope
carries the Admin values -- and that it holds real credentials, because Snapshots do (§12).
"""

import asyncio
import json

from httpx import AsyncClient

from app.pipeline.applies import TERMINAL
from app.pipeline.configuration import FORMAT
from app.sections.registry import SECTIONS

SNAPSHOT_EXPORT = "/api/v1/snapshots/{number}/export"
CANDIDATE_EXPORT = "/api/v1/candidate/export"
ADMIN_EXPORT = "/api/v1/admin/configuration/export"

#: A password in a property value, so "the document holds credentials" is asserted on the
#: bytes rather than inferred from the schema.
PASSWORD = "s3cret-not-a-reference"
CATALOG = {
    "name": "finance",
    "connector": "postgresql",
    "properties": {"connection-url": f"jdbc:postgresql://db:5432/f?password={PASSWORD}"},
}


async def _settled(client: AsyncClient, apply_id: str) -> dict:
    deadline = asyncio.get_running_loop().time() + 180
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.02)
    raise AssertionError("never settled")


async def _apply(client: AsyncClient) -> int:
    record = await _settled(client, (await client.post("/api/v1/applies")).json()["id"])
    assert record["stage"] == "succeeded", record["failure_reason"]
    number: int = record["snapshot"]
    return number


async def test_a_candidate_export_names_itself_and_holds_every_section(
    client: AsyncClient,
) -> None:
    """The `format` is what lets a reader refuse a file that is not one of these, and every
    Section is present even when empty -- an import refuses a document missing one."""
    response = await client.get(CANDIDATE_EXPORT)

    assert response.status_code == 200, response.json()
    document = response.json()
    assert document["format"] == FORMAT
    assert set(document["sections"]) == set(SECTIONS)
    assert document["provenance"]["source"] == "candidate"
    assert document["provenance"]["apchi_version"], "a human has to be able to judge the file"


async def test_a_candidate_export_carries_no_admin_values(client: AsyncClient) -> None:
    """The one difference between the scopes. An Operator cannot set them, so an Operator's
    document must not carry them -- and an Operator's import must not apply them."""
    document = (await client.get(CANDIDATE_EXPORT)).json()

    assert document["admin"] is None


async def test_the_admin_export_carries_them(client: AsyncClient) -> None:
    """What makes the Admin scope the *full* configuration: Admin values are applied
    through the pipeline and never recorded in a Snapshot (invariant 9, §14), so without
    them a document is less than what the Cluster runs."""
    assert (
        await client.put("/api/v1/admin/permissions/enforcement", json={"enforced": True})
    ).status_code == 200

    document = (await client.get(ADMIN_EXPORT)).json()

    assert document["admin"]["enforce_permissions"] is True
    assert set(document["sections"]) == set(SECTIONS)


async def test_a_snapshot_export_is_that_snapshots_configuration(
    applying_client: AsyncClient,
) -> None:
    assert (await applying_client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201
    number = await _apply(applying_client)

    document = (await applying_client.get(SNAPSHOT_EXPORT.format(number=number))).json()

    assert document["provenance"]["source"] == "snapshot"
    assert document["provenance"]["snapshot"] == number
    assert set(document["sections"]["catalogs"]) == {"finance"}


async def test_a_snapshot_that_does_not_exist_is_404(client: AsyncClient) -> None:
    response = await client.get(SNAPSHOT_EXPORT.format(number=77))

    assert response.status_code == 404, response.json()


async def test_the_candidate_export_holds_the_staged_edit_no_snapshot_has(
    applying_client: AsyncClient,
) -> None:
    """The case the Candidate export exists for: an edit that has not been applied has
    never been committed, so no Snapshot holds it."""
    assert (await applying_client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201
    number = await _apply(applying_client)
    assert (
        await applying_client.post(
            "/api/v1/catalogs", json={"name": "staged", "connector": "memory"}
        )
    ).status_code == 201

    document = (await applying_client.get(CANDIDATE_EXPORT)).json()
    committed = (await applying_client.get(SNAPSHOT_EXPORT.format(number=number))).json()

    assert "staged" in document["sections"]["catalogs"]
    assert "staged" not in committed["sections"]["catalogs"], (
        "the Snapshot predates the edit, which is why the Candidate is exported at all"
    )


async def test_a_document_holds_real_credentials(applying_client: AsyncClient) -> None:
    """Decided deliberately, and it is what makes an imported configuration work rather
    than need re-entering. Asserted on the serialised bytes: a reference in place of the
    value would pass a schema check and produce a configuration that cannot connect."""
    assert (await applying_client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201
    number = await _apply(applying_client)

    document = json.dumps((await applying_client.get(SNAPSHOT_EXPORT.format(number=number))).json())

    assert PASSWORD in document

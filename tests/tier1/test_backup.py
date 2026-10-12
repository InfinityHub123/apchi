"""The export: everything MongoDB holds that the Cluster cannot say again.

#116 established that re-adoption recovers the configuration after a MongoDB loss, which
narrows what an export is for. These tests are about the remainder -- the Snapshots nothing
in Kubernetes holds, the Admin values no Snapshot records, and the staged edits that were
never on the Cluster -- and about the one thing the document must say about itself: it holds
real credentials, because Snapshots do (§12).
"""

import asyncio
import json

from httpx import AsyncClient
from pymongo import AsyncMongoClient

from app.config import Settings
from app.pipeline.applies import TERMINAL, ApplyStore
from app.pipeline.backup import FORMAT

BACKUP = "/api/v1/admin/backup"

#: A password in a property value, so "the export holds credentials" can be asserted on the
#: bytes rather than inferred from the schema.
PASSWORD = "s3cret-rotated-away-years-ago"
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


async def test_a_fresh_install_exports_an_honest_nothing(client: AsyncClient) -> None:
    """Before anything is configured there is nothing to lose, and the export says so.

    A null Candidate rather than a derived one: `load` would invent a Candidate here, and an
    export that invented one would claim to be preserving something it is not.
    """
    response = await client.get(BACKUP)

    assert response.status_code == 200, response.json()
    backup = response.json()
    assert backup["format"] == FORMAT
    assert backup["snapshots"] == []
    assert backup["candidate"] is None
    assert backup["snapshots_ever"] is None
    assert backup["cluster_id"] is None
    assert backup["apchi_version"], "provenance is what makes a restore judgeable"


async def test_the_export_holds_every_snapshot_oldest_first(applying_client: AsyncClient) -> None:
    """Every one, not the newest and not a page of them.

    `/snapshots` is paged at 50 because an Operator reading history wants the recent end.
    An export paged at 50 is a history with a hole in it, and the hole is silent.
    """
    assert (await applying_client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201
    first = await _apply(applying_client)
    assert (
        await applying_client.post("/api/v1/catalogs", json={"name": "b", "connector": "memory"})
    ).status_code == 201
    second = await _apply(applying_client)

    backup = (await applying_client.get(BACKUP)).json()

    assert [s["number"] for s in backup["snapshots"]] == [first, second], (
        "oldest first, because that is the order they have to be written back in for their "
        "numbers to come out the same"
    )
    assert set(backup["snapshots"][0]["sections"]["catalogs"]) == {"finance"}
    assert set(backup["snapshots"][1]["sections"]["catalogs"]) == {"finance", "b"}


async def test_the_export_holds_the_staged_edit_nothing_else_can(
    applying_client: AsyncClient,
) -> None:
    """The one thing no recovery from the Cluster reaches.

    #116's recovery reads Apchi's output back off the coordinator, so it can only see what
    was applied. An edit staged and not applied was never there. This is the export's
    narrowest and strongest claim.
    """
    assert (await applying_client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201
    await _apply(applying_client)
    assert (
        await applying_client.post(
            "/api/v1/catalogs", json={"name": "staged", "connector": "memory"}
        )
    ).status_code == 201

    backup = (await applying_client.get(BACKUP)).json()

    assert "staged" in backup["candidate"]["sections"]["catalogs"]
    assert backup["candidate"]["base_snapshot"] == 1
    assert all("staged" not in s["sections"]["catalogs"] for s in backup["snapshots"]), (
        "and it is in no Snapshot, which is exactly why losing it is unrecoverable"
    )


async def test_the_export_holds_admin_values_that_no_snapshot_records(
    applying_client: AsyncClient,
) -> None:
    """Invariant 9: Admin values are applied and never recorded in a Snapshot.

    So nothing else in Apchi records them, and an export of Snapshots alone would quietly
    lose an Admin's enforcement decision and every preserved pattern a migration depends on.
    """
    assert (
        await applying_client.put("/api/v1/admin/permissions/enforcement", json={"enforced": True})
    ).status_code == 200
    preserved = {"patterns": [{"pattern": "CN=(.*),OU=legacy", "user": "$1", "case": "lower"}]}
    assert (
        await applying_client.put("/api/v1/admin/certificate-mapping/preserved", json=preserved)
    ).status_code == 200
    assert (await applying_client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201
    await _apply(applying_client)

    backup = (await applying_client.get(BACKUP)).json()

    assert backup["admin_values"]["enforce_permissions"] is True
    assert backup["admin_values"]["preserved_certificate_mappings"] == preserved["patterns"]
    assert all("admin" not in s["sections"] for s in backup["snapshots"]), (
        "the Snapshot does not carry them, which is the reason this field has to exist"
    )


async def test_the_export_holds_maintenance_mode(client: AsyncClient) -> None:
    """Apchi engages Maintenance Mode itself after a failed Auto Rollback.

    Losing that on a restore would reopen a Cluster whose state is unknown to the Operators
    the mode exists to keep out.
    """
    engaged = await client.put(
        "/api/v1/admin/maintenance-mode",
        json={"engaged": True, "reason": "auto rollback failed"},
    )
    assert engaged.status_code == 200, engaged.json()

    backup = (await client.get(BACKUP)).json()

    assert backup["maintenance"]["engaged"] is True
    assert backup["maintenance"]["reason"] == "auto rollback failed"
    assert backup["maintenance"]["engaged_by"] == "admin"


async def test_the_export_is_every_credential_the_cluster_has_ever_used(
    applying_client: AsyncClient,
) -> None:
    """§12, asserted on the bytes rather than left to the documentation.

    Snapshots store actual secret values, not references, and they are immutable. So a
    password is in here in full, and it stays in here after it is rotated away -- which is
    the whole reason this is on the Admin surface and the docs say so in those words.
    """
    assert (await applying_client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201
    await _apply(applying_client)
    rotated = await applying_client.patch(
        "/api/v1/catalogs/finance",
        json={"properties": {"connection-url": "jdbc:postgresql://db:5432/f"}},
    )
    assert rotated.status_code == 200, rotated.json()
    assert PASSWORD not in json.dumps(rotated.json()), "the live Catalog no longer holds it"
    await _apply(applying_client)

    document = json.dumps((await applying_client.get(BACKUP)).json())

    assert document.count(PASSWORD) == 1, (
        "the rotated-away password is still in the export, in the Snapshot that was taken "
        "while it was live. Immutability means it cannot be purged from one"
    )


async def test_the_export_leaves_out_records_of_activity(applying_client: AsyncClient) -> None:
    """Not a database dump.

    `applies`, `validations` and `discoveries` record what happened rather than what is
    configured. Including them would make this `mongodump` with extra steps, and worse at it.
    """
    assert (await applying_client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201
    await _apply(applying_client)
    assert (await applying_client.get("/api/v1/applies")).json(), "an Apply was recorded"

    backup = (await applying_client.get(BACKUP)).json()

    assert set(backup) == {
        "format",
        "taken_at",
        "apchi_version",
        "cluster_id",
        "snapshots_ever",
        "snapshots",
        "candidate",
        "admin_values",
        "maintenance",
    }


async def test_the_export_carries_the_history_it_belongs_to(applying_client: AsyncClient) -> None:
    """Provenance from #112's mark: which install this is, and how much of it this is.

    `snapshots_ever` higher than the newest Snapshot below means the export was itself taken
    after a loss. A restore that cannot tell the difference would silently install a
    truncated history as if it were the whole one (#114).
    """
    assert (await applying_client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201
    await _apply(applying_client)

    backup = (await applying_client.get(BACKUP)).json()

    assert backup["snapshots_ever"] == 1
    assert backup["cluster_id"], "minted on the first commit, and the same one from then on"
    assert backup["snapshots_ever"] == backup["snapshots"][-1]["number"], (
        "equal, so this export is the whole history rather than part of one"
    )


async def test_an_export_taken_after_a_loss_admits_it_is_incomplete(
    applying_client: AsyncClient, settings: Settings
) -> None:
    """The case the previous test's equality exists to catch.

    An Admin who loses MongoDB and takes an export before restoring a backup gets a document
    with no Snapshots in it. It must not look like a fresh install's export, because
    restoring it would be writing the loss down as the truth.
    """
    assert (await applying_client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201
    await _apply(applying_client)
    mongo: AsyncMongoClient = AsyncMongoClient(settings.mongo_uri)
    await mongo.drop_database(settings.mongo_database)
    await mongo.close()

    backup = (await applying_client.get(BACKUP)).json()

    assert backup["snapshots"] == []
    assert backup["snapshots_ever"] == 1, (
        "the high-water mark survives in a ConfigMap, so the export knows a Snapshot 1 "
        "existed even though MongoDB no longer holds it"
    )


async def test_an_export_is_refused_while_an_apply_is_in_flight(
    client: AsyncClient, settings: Settings
) -> None:
    """Refused rather than taken at a coherent-enough point, and this is where that is fixed.

    An Apply promotes the Candidate to a Snapshot. Reading across that moment could produce
    a document holding a Candidate mid-promotion, and the cheap honest answer to a request
    that would need a lock is to say no.
    """
    mongo: AsyncMongoClient = AsyncMongoClient(settings.mongo_uri)
    record = await ApplyStore(mongo[settings.mongo_database]).create(base_snapshot=None)

    try:
        refused = await client.get(BACKUP)
    finally:
        await mongo.close()

    assert refused.status_code == 409
    assert refused.json()["code"] == "conflict"
    assert record.id in refused.json()["message"]

"""Adopting a Cluster Apchi itself configured: the round-trip of its own output.

§15's Adoption is written for a Cluster configured by hand, and #87-#89 are tested against
that. This is the other case, and the easy one: Apchi reading back files Apchi wrote. It is
worth asserting precisely because it looks trivial -- the claim is that every Section comes
back *equal*, not similar, and anything less would be a property quietly dropped on a path
nobody looks at.

It is reachable in practice: point an Apchi at a database that does not hold the Snapshots
for the Cluster in front of it, and adopting is what you would do. The Adoption gate makes
that deliberate rather than accidental (`discard_history_through`), and these tests go
through it.

What a re-adoption cannot produce is the numbered history behind the Snapshot it creates, or
a Candidate's staged-but-unapplied edits -- those were never on the Cluster to read.
"""

import asyncio

from httpx import AsyncClient
from pymongo import AsyncMongoClient

from app.config import Settings
from app.pipeline.applies import TERMINAL
from tests.conftest import FakeKubernetes

BASE = "/api/v1/admin/adoption"

CATALOG = {
    "name": "finance",
    "connector": "postgresql",
    # A credential-bearing property, because the question worth answering is not whether a
    # catalog comes back by name but whether it comes back usable.
    "properties": {"connection-url": "jdbc:postgresql://db:5432/f"},
}
GRANT = {"identity": "analyst", "catalog": "finance", "privileges": ["SELECT"]}
GROUP = {"path": "reports", "hard_concurrency_limit": 20, "max_queued": 100}


async def _settled(client: AsyncClient, apply_id: str) -> dict:
    deadline = asyncio.get_running_loop().time() + 180
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.02)
    raise AssertionError("never settled")


async def _configured_cluster(client: AsyncClient) -> dict:
    """A Cluster Apchi manages, with something in several Sections. Returns its Snapshot."""
    assert (await client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201
    assert (await client.post("/api/v1/permissions", json=GRANT)).status_code == 201
    assert (await client.post("/api/v1/resource-groups", json=GROUP)).status_code == 201

    record = await _settled(client, (await client.post("/api/v1/applies")).json()["id"])
    assert record["stage"] == "succeeded", record["failure_reason"]
    return (await client.get(f"/api/v1/snapshots/{record['snapshot']}")).json()


async def _empty_the_database(settings: Settings) -> None:
    """An Apchi looking at a database that does not hold this Cluster's Snapshots.

    Dropped rather than repointed because the two are indistinguishable from Apchi's side,
    and dropping is the one a test can do.
    """
    client: AsyncMongoClient = AsyncMongoClient(settings.mongo_uri)
    await client.drop_database(settings.mongo_database)
    await client.close()


async def test_re_adoption_recovers_the_configuration_that_was_lost(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    """The assertion this whole path rests on: equal, not merely similar.

    Every Section, compared whole. Anything less -- "the catalog is there", "the grant
    looks right" -- would pass while silently dropping a property, and a dropped property
    in a `connection-url` is a catalog that exists and cannot be queried.
    """
    lost = await _configured_cluster(applying_client)
    await _empty_the_database(settings)

    # The mark says this Cluster has committed Snapshots, so Adoption refuses until told
    # the number -- which is why this goes through the gate rather than around it.
    refused = await applying_client.post(f"{BASE}/adopt")
    assert refused.status_code == 409
    assert f"discard_history_through={lost['number']}" in refused.json()["message"]

    created = await applying_client.post(f"{BASE}/discoveries")
    assert created.status_code == 201, created.json()
    assert created.json()["complete"], created.json()["outstanding"]

    seeded = await applying_client.post(f"{BASE}/seed")
    assert seeded.status_code == 200, seeded.json()
    assert seeded.json()["changed"] == [], (
        "nothing should need writing: the Secrets already hold what the discovery just read "
        "out of them, because Apchi wrote them"
    )

    started = await applying_client.post(f"{BASE}/adopt?discard_history_through={lost['number']}")
    assert started.status_code == 202, started.json()
    record = await _settled(applying_client, started.json()["id"])
    assert record["stage"] == "succeeded", record["failure_reason"]

    recovered = (await applying_client.get("/api/v1/snapshots/1")).json()
    assert recovered["sections"] == lost["sections"], (
        "the recovered Snapshot must be the one that was lost, section for section. This is "
        "what the normalisation in the Resource Groups generator is for: an absent "
        "'#settings' and an all-unset one are the same configuration, and writing one on "
        "the Operator's path while omitting it on the parser's made two identical Clusters "
        "produce unequal Snapshots"
    )


async def test_the_recovered_cluster_is_managed_again(
    applying_client: AsyncClient, settings: Settings
) -> None:
    """Recovery has to leave Apchi able to work, not just able to describe the Cluster.

    The Candidate is re-derived from the new Snapshot, so the next ordinary edit applies on
    top of what the Cluster is actually running rather than on top of nothing.
    """
    lost = await _configured_cluster(applying_client)
    await _empty_the_database(settings)
    await applying_client.post(f"{BASE}/discoveries")
    await applying_client.post(f"{BASE}/seed")
    started = await applying_client.post(f"{BASE}/adopt?discard_history_through={lost['number']}")
    await _settled(applying_client, started.json()["id"])

    review = (await applying_client.get("/api/v1/review")).json()

    assert review["has_changes"] is False, (
        "the Candidate is the Snapshot just recovered, so there is nothing staged -- an "
        "Operator arriving after a recovery sees a managed Cluster, not a pending diff"
    )
    assert review["base_snapshot"] == 1, (
        "and it is derived from the recovered Snapshot, so the next edit applies on top of "
        "what the Cluster is actually running rather than on top of nothing"
    )


async def test_what_recovery_does_not_bring_back(
    applying_client: AsyncClient, settings: Settings
) -> None:
    """The cost, asserted rather than left to the documentation.

    Two things are gone for good, and both follow from where they lived: the earlier
    Snapshots, because nothing in Kubernetes holds them, and a staged edit, because it was
    never applied to the Cluster and so was never anywhere the Cluster could be read from.
    """
    first = await _configured_cluster(applying_client)
    assert (
        await applying_client.post("/api/v1/catalogs", json={"name": "b", "connector": "memory"})
    ).status_code == 201
    record = await _settled(
        applying_client, (await applying_client.post("/api/v1/applies")).json()["id"]
    )
    assert record["snapshot"] == first["number"] + 1

    # Staged and deliberately not applied: this is the edit no recovery can reach.
    assert (
        await applying_client.post(
            "/api/v1/catalogs", json={"name": "staged", "connector": "memory"}
        )
    ).status_code == 201

    await _empty_the_database(settings)
    await applying_client.post(f"{BASE}/discoveries")
    await applying_client.post(f"{BASE}/seed")
    started = await applying_client.post(f"{BASE}/adopt?discard_history_through=2")
    await _settled(applying_client, started.json()["id"])

    numbers = [
        s["number"] for s in (await applying_client.get("/api/v1/snapshots")).json()["items"]
    ]
    assert numbers == [1], (
        "two Snapshots were committed and a re-adoption produces one. §11's Section Revert "
        "and Full Rollback have nothing behind them afterwards, which is what makes this "
        "deliberate rather than routine"
    )

    recovered = (await applying_client.get("/api/v1/snapshots/1")).json()
    assert "staged" not in recovered["sections"]["catalogs"], (
        "the staged Catalog was never applied, so it was never on the Cluster, so there is "
        "nowhere to read it back from"
    )
    assert set(recovered["sections"]["catalogs"]) == {"finance", "b"}

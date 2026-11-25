"""A Snapshot deleted from MongoDB by hand.

Apchi never deletes a Snapshot, but nothing stops somebody doing it in MongoDB. Two things
used to follow from deleting the latest one:

* **its number came back.** The next commit was `latest + 1`, so after deleting Snapshot N the
  next one was N again -- and every Apply record and audit event that named N pointed at a
  different configuration
* **the Candidate derived from it read as an empty baseline**, so Review showed the whole
  configuration as added and an Apply planned it as new

Now the number is never reused, and a Candidate whose base is gone is refused by Review and
Apply alike, naming the way out -- a Reset, which re-derives from the latest Snapshot.
Deleting an *older* Snapshot stays harmless, because nothing compares against it.
"""

import asyncio

from httpx import AsyncClient

from app.pipeline.applies import TERMINAL
from tests.conftest import FakeKubernetes

CATALOG = {"name": "finance", "connector": "memory", "properties": {}}


async def _apply(client: AsyncClient) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.text
    deadline = asyncio.get_running_loop().time() + 180
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return dict(record)
        await asyncio.sleep(0.05)
    raise AssertionError("the Apply never settled")


def _database(client: AsyncClient) -> object:
    return client._transport.app.state.mongo.database  # type: ignore[attr-defined]


async def _delete_snapshot(client: AsyncClient, number: int) -> None:
    result = await _database(client)["snapshots"].delete_one({"_id": number})  # type: ignore[index]
    assert result.deleted_count == 1


async def test_review_refuses_a_candidate_whose_base_is_gone_and_names_the_way_out(
    applying_client: AsyncClient,
) -> None:
    await applying_client.post("/api/v1/catalogs", json=CATALOG)
    assert (await _apply(applying_client))["snapshot"] == 1
    await _delete_snapshot(applying_client, 1)

    review = await applying_client.get("/api/v1/review")

    assert review.status_code == 409, review.text
    assert "Snapshot 1 is no longer in MongoDB" in review.json()["message"]
    assert "POST /api/v1/candidate/reset" in review.json()["message"]


async def test_an_apply_on_a_missing_base_is_refused_before_it_touches_the_cluster(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Against an empty baseline it would have planned `finance` as new and issued its DDL."""
    await applying_client.post("/api/v1/catalogs", json=CATALOG)
    await _apply(applying_client)
    seed_before = dict(fake_kubernetes.secrets["trino-catalog-seed"])
    await _delete_snapshot(applying_client, 1)

    record = await _apply(applying_client)

    assert record["stage"] == "failed", record
    assert "Snapshot 1 is no longer in MongoDB" in record["failure_reason"]
    assert record["rolled_out"] is False
    assert fake_kubernetes.secrets["trino-catalog-seed"] == seed_before


async def test_a_reset_re_derives_from_the_latest_and_lets_the_next_apply_through(
    applying_client: AsyncClient,
) -> None:
    await applying_client.post("/api/v1/catalogs", json=CATALOG)
    await _apply(applying_client)
    await applying_client.post("/api/v1/catalogs", json=CATALOG | {"name": "sales"})
    await _apply(applying_client)
    await _delete_snapshot(applying_client, 2)

    reset = await applying_client.post("/api/v1/candidate/reset")

    assert reset.status_code in (200, 204), reset.text
    review = await applying_client.get("/api/v1/review")
    assert review.status_code == 200, review.text
    assert review.json()["base_snapshot"] == 1
    assert (await _apply(applying_client))["stage"] == "succeeded"


async def test_a_deleted_latest_snapshots_number_is_never_reused(
    applying_client: AsyncClient,
) -> None:
    await _apply(applying_client)
    await _apply(applying_client)
    await _delete_snapshot(applying_client, 2)
    await applying_client.post("/api/v1/candidate/reset")

    record = await _apply(applying_client)

    assert record["snapshot"] == 3, "Snapshot 2's number came back for a different configuration"


async def test_an_install_from_before_the_counter_does_not_reuse_one_either(
    applying_client: AsyncClient,
) -> None:
    """No counter yet, and the latest Snapshot deleted: the Apply records still name it."""
    await _apply(applying_client)
    await _apply(applying_client)
    await _database(applying_client)["counters"].delete_many({})  # type: ignore[index]
    await _delete_snapshot(applying_client, 2)
    await applying_client.post("/api/v1/candidate/reset")

    record = await _apply(applying_client)

    assert record["snapshot"] == 3


async def test_deleting_an_older_snapshot_changes_nothing(applying_client: AsyncClient) -> None:
    """Nothing compares against it: the Candidate is derived from the latest."""
    await _apply(applying_client)
    await _apply(applying_client)
    await _delete_snapshot(applying_client, 1)

    assert (await applying_client.get("/api/v1/review")).status_code == 200
    assert (await _apply(applying_client))["snapshot"] == 3
    assert (await applying_client.get("/api/v1/snapshots/1")).status_code == 404

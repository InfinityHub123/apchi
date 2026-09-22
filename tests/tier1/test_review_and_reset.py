"""Review and Reset."""

from httpx import AsyncClient

PG = {
    "name": "finance",
    "connector": "postgresql",
    "properties": {"connection-url": "jdbc:postgresql://db:5432/f", "connection-user": "t"},
}


async def test_review_of_an_untouched_candidate_reports_no_changes(client: AsyncClient) -> None:
    review = (await client.get("/api/v1/review")).json()

    assert review["has_changes"] is False
    assert [s["section"] for s in review["sections"]] == ["catalogs"]


async def test_review_reports_a_staged_catalog_as_added(client: AsyncClient) -> None:
    await client.post("/api/v1/catalogs", json=PG)

    review = (await client.get("/api/v1/review")).json()

    assert review["has_changes"] is True
    changes = review["sections"][0]["changes"]
    assert [(c["resource"], c["change"]) for c in changes] == [("finance", "added")]


async def test_reset_discards_everything_staged(client: AsyncClient) -> None:
    await client.post("/api/v1/catalogs", json=PG)

    assert (await client.post("/api/v1/candidate/reset")).status_code == 204

    assert (await client.get("/api/v1/catalogs")).json() == []
    assert (await client.get("/api/v1/review")).json()["has_changes"] is False


async def test_reset_reaches_nothing_outside_apchi(client: AsyncClient, fake_kubernetes) -> None:
    """Nothing reaches the Cluster before Apply, so Reset cannot affect production."""
    await client.post("/api/v1/catalogs", json=PG)

    await client.post("/api/v1/candidate/reset")

    assert fake_kubernetes.secrets == {}


async def test_the_candidate_survives_across_requests(client: AsyncClient) -> None:
    """It is a singleton in MongoDB, not per-request state."""
    await client.post("/api/v1/catalogs", json=PG)
    await client.post("/api/v1/catalogs", json={**PG, "name": "hr"})

    assert [c["name"] for c in (await client.get("/api/v1/catalogs")).json()] == ["finance", "hr"]

"""Catalogs in the Configuration Candidate, through the HTTP API.

Nothing here should reach Trino or Kubernetes: staging a Catalog is a change to
Apchi's own state, and Apply is what makes it real.
"""

from httpx import AsyncClient

from tests.conftest import bootstrap_secrets

PG = {
    "name": "finance",
    "connector": "postgresql",
    "properties": {
        "connection-url": "jdbc:postgresql://db.internal:5432/finance",
        "connection-user": "trino",
        "connection-password": "hunter2",
    },
}


async def test_a_staged_catalog_is_listed_and_fetchable(client: AsyncClient) -> None:
    created = await client.post("/api/v1/catalogs", json=PG)
    assert created.status_code == 201
    assert created.json()["name"] == "finance"

    assert [c["name"] for c in (await client.get("/api/v1/catalogs")).json()] == ["finance"]
    assert (await client.get("/api/v1/catalogs/finance")).json()["connector"] == "postgresql"


async def test_staging_a_catalog_reaches_nothing_else(client: AsyncClient, fake_kubernetes) -> None:
    await client.post("/api/v1/catalogs", json=PG)

    assert fake_kubernetes.secrets == bootstrap_secrets()


async def test_a_catalog_can_be_edited(client: AsyncClient) -> None:
    await client.post("/api/v1/catalogs", json=PG)

    edited = await client.patch(
        "/api/v1/catalogs/finance",
        json={"properties": {**PG["properties"], "connection-user": "reader"}},
    )

    assert edited.status_code == 200
    assert edited.json()["properties"]["connection-user"] == "reader"


async def test_a_catalog_can_be_removed(client: AsyncClient) -> None:
    await client.post("/api/v1/catalogs", json=PG)

    assert (await client.delete("/api/v1/catalogs/finance")).status_code == 204
    assert (await client.get("/api/v1/catalogs")).json() == []


async def test_a_duplicate_name_is_a_conflict(client: AsyncClient) -> None:
    await client.post("/api/v1/catalogs", json=PG)

    clash = await client.post("/api/v1/catalogs", json=PG)

    assert clash.status_code == 409
    assert clash.json()["code"] == "name_already_taken"


async def test_an_unknown_catalog_is_not_found(client: AsyncClient) -> None:
    missing = await client.get("/api/v1/catalogs/nope")

    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


async def test_an_invalid_name_is_rejected(client: AsyncClient) -> None:
    rejected = await client.post("/api/v1/catalogs", json={**PG, "name": "Not A Name"})

    assert rejected.status_code == 422
    assert rejected.json()["code"] == "unprocessable_payload"


async def test_errors_carry_a_request_id(client: AsyncClient) -> None:
    response = await client.get("/api/v1/catalogs/nope")

    assert response.json()["request_id"]
    assert response.headers["X-Request-Id"] == response.json()["request_id"]


async def test_a_rejected_payload_never_enters_the_candidate(client: AsyncClient) -> None:
    """A payload that gets a 201 becomes every Operator's problem, because the
    Candidate is shared. A rejected one must leave no trace."""
    await client.post("/api/v1/catalogs", json={**PG, "name": "Not A Name"})

    assert (await client.get("/api/v1/catalogs")).json() == []


async def test_a_catalog_can_present_a_client_certificate(client: AsyncClient) -> None:
    """The Operator names the certificate; Apchi puts it into the properties the way this
    connector expects, so nobody types a path."""
    created = await client.post(
        "/api/v1/catalogs",
        json={
            "name": "finance",
            "connector": "postgresql",
            "properties": {"connection-url": "jdbc:postgresql://db:5432/x?sslmode=verify-full"},
            "certificate": "finance",
        },
    )

    assert created.status_code == 201, created.json()
    assert created.json()["certificate"] == "finance"


async def test_a_certificate_on_a_connector_apchi_cannot_wire_is_refused(
    client: AsyncClient,
) -> None:
    """Accepted and not wired would be the worst outcome available: a Catalog that connects
    without the certificate the Operator asked it to present."""
    refused = await client.post(
        "/api/v1/catalogs",
        json={
            "name": "events",
            "connector": "kafka",
            "properties": {"kafka.table-names": "t", "kafka.nodes": "k:9092"},
            "certificate": "finance",
        },
    )

    assert refused.status_code == 422
    assert "${cert:finance}" in refused.json()["message"], "the escape hatch is named"


async def test_a_certificate_without_tls_configured_is_refused(client: AsyncClient) -> None:
    """A client certificate does nothing until the connection is told to use TLS, and Apchi
    will not choose between `require` and `verify-full` on an Operator's behalf."""
    refused = await client.post(
        "/api/v1/catalogs",
        json={
            "name": "finance",
            "connector": "postgresql",
            "properties": {"connection-url": "jdbc:postgresql://db:5432/x"},
            "certificate": "finance",
        },
    )

    assert refused.status_code == 422
    assert "sslmode" in refused.json()["message"]

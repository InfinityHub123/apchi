"""Curated connector schemas, and the pass-through escape hatch.

The point of curating is catching a typo at request time rather than at Apply. The
point of pass-through is that an unusual connector is not blocked entirely.
"""

from httpx import AsyncClient

# Deliberately not in the curated set, and unlikely to join it.
UNCURATED = "clickhouse"


async def test_an_uncurated_connector_passes_through(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/catalogs",
        json={
            "name": "events",
            "connector": UNCURATED,
            "properties": {"anything-at-all": "is accepted here"},
        },
    )

    assert created.status_code == 201
    assert created.json()["properties"] == {"anything-at-all": "is accepted here"}


async def test_pass_through_is_marked_unsupported(client: AsyncClient) -> None:
    """So nobody mistakes the escape hatch for a curated path."""
    created = await client.post(
        "/api/v1/catalogs",
        json={"name": "events", "connector": UNCURATED, "properties": {}},
    )

    assert created.json()["supported"] is False


async def test_references_to_things_apchi_cannot_see_are_accepted(client: AsyncClient) -> None:
    """Cross-resource references are checked at Validate, over the whole Candidate,
    not at request time. Enforcing them here would make the order an Operator fills
    in a screen matter."""
    created = await client.post(
        "/api/v1/catalogs",
        json={
            "name": "events",
            "connector": UNCURATED,
            "properties": {"ssl-cert": "/etc/trino/certs/not-uploaded-yet.crt"},
        },
    )

    assert created.status_code == 201


PG_URL = "jdbc:postgresql://db.internal:5432/finance"


async def test_a_curated_connector_is_marked_supported(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/catalogs",
        json={
            "name": "finance",
            "connector": "postgresql",
            "properties": {"connection-url": PG_URL},
        },
    )

    assert created.status_code == 201
    assert created.json()["supported"] is True


async def test_a_typo_is_caught_at_request_time(client: AsyncClient) -> None:
    """The whole point of curating: this would otherwise surface at Apply."""
    rejected = await client.post(
        "/api/v1/catalogs",
        json={
            "name": "finance",
            "connector": "postgresql",
            "properties": {"connection-uri": PG_URL},
        },
    )

    assert rejected.status_code == 422
    problems = [d["problem"] for d in rejected.json()["details"]]
    assert any("did you mean 'connection-url'" in p for p in problems)


async def test_a_missing_required_property_is_rejected(client: AsyncClient) -> None:
    rejected = await client.post(
        "/api/v1/catalogs",
        json={"name": "finance", "connector": "postgresql", "properties": {}},
    )

    assert rejected.status_code == 422
    assert any("required" in d["problem"] for d in rejected.json()["details"])


async def test_redis_table_names_is_optional(client: AsyncClient) -> None:
    """It appears in Trino's minimal example but the docs say it is optional when
    redis.table-description-dir is used. Marking it required would reject valid
    configurations."""
    created = await client.post(
        "/api/v1/catalogs",
        json={"name": "cache", "connector": "redis", "properties": {"redis.nodes": "h:6379"}},
    )

    assert created.status_code == 201


async def test_a_selector_brings_further_properties_into_play(client: AsyncClient) -> None:
    """iceberg.catalog.type=rest requires the REST URI; hive_metastore does not."""
    rejected = await client.post(
        "/api/v1/catalogs",
        json={
            "name": "lake",
            "connector": "iceberg",
            "properties": {"iceberg.catalog.type": "rest"},
        },
    )

    assert rejected.status_code == 422
    assert any("iceberg.rest-catalog.uri" in d["problem"] for d in rejected.json()["details"])


async def test_an_invalid_selector_value_is_rejected(client: AsyncClient) -> None:
    rejected = await client.post(
        "/api/v1/catalogs",
        json={
            "name": "lake",
            "connector": "iceberg",
            "properties": {"iceberg.catalog.type": "postgres"},
        },
    )

    assert rejected.status_code == 422
    assert any("not a valid value" in d["problem"] for d in rejected.json()["details"])


async def test_a_selector_default_applies_when_unset(client: AsyncClient) -> None:
    """hive.metastore defaults to thrift, which requires the metastore URI."""
    rejected = await client.post(
        "/api/v1/catalogs",
        json={"name": "warehouse", "connector": "hive", "properties": {}},
    )

    assert rejected.status_code == 422
    assert any("hive.metastore.uri" in d["problem"] for d in rejected.json()["details"])

"""Grants in the Configuration Candidate.

Staging only. What is interesting here is the translation: an Operator writes literal names
and Apchi writes anchored regular expressions, because Trino matches these fields as patterns
and an unanchored `finance` would also grant on `finance_archive`.
"""

import json

from httpx import AsyncClient

from app.sections.permissions.generator import render_rules

READ_NATION = {
    "identity": "acme_finance",
    "catalog": "iceberg",
    "schema": "finance",
    "table": "transactions",
    "privileges": ["SELECT"],
}
READ_SCHEMA = {
    "identity": "acme_finance",
    "catalog": "iceberg",
    "schema": "finance",
    "privileges": ["SELECT"],
}
OWN_CATALOG = {"identity": "etl", "catalog": "staging", "privileges": ["SELECT", "INSERT"]}


async def test_a_staged_grant_is_listed_with_the_key_it_is_addressed_by(
    client: AsyncClient,
) -> None:
    created = await client.post("/api/v1/permissions", json=READ_NATION)

    assert created.status_code == 201
    assert created.json()["key"] == "acme_finance:iceberg:finance:transactions"
    listed = (await client.get("/api/v1/permissions")).json()
    assert [grant["key"] for grant in listed] == ["acme_finance:iceberg:finance:transactions"]


async def test_a_grant_may_name_a_catalog_a_schema_or_a_table(client: AsyncClient) -> None:
    for grant in (OWN_CATALOG, READ_SCHEMA, READ_NATION):
        created = await client.post("/api/v1/permissions", json=grant)
        assert created.status_code == 201, created.json()

    keys = [grant["key"] for grant in (await client.get("/api/v1/permissions")).json()]
    assert keys == [
        "acme_finance:iceberg:finance:*",
        "acme_finance:iceberg:finance:transactions",
        "etl:staging:*:*",
    ]


async def test_a_grant_naming_no_catalog_is_refused(client: AsyncClient) -> None:
    refused = await client.post(
        "/api/v1/permissions", json={"identity": "etl", "privileges": ["SELECT"]}
    )

    assert refused.status_code == 422
    assert "catalog" in str(refused.json()["details"])


async def test_a_table_without_its_schema_is_refused(client: AsyncClient) -> None:
    """`orders` in some schema, or every schema's `orders`? Neither is what anybody meant."""
    refused = await client.post(
        "/api/v1/permissions",
        json={"identity": "etl", "catalog": "staging", "table": "orders", "privileges": ["SELECT"]},
    )

    assert refused.status_code == 422
    assert "schema" in str(refused.json()["details"])


async def test_a_privilege_trino_does_not_have_is_refused(client: AsyncClient) -> None:
    """Trino's six, and no others: an unknown one fails the coordinator's startup."""
    refused = await client.post(
        "/api/v1/permissions",
        json={"identity": "etl", "catalog": "staging", "privileges": ["TRUNCATE"]},
    )

    assert refused.status_code == 422


async def test_a_grant_with_no_privileges_is_refused(client: AsyncClient) -> None:
    refused = await client.post(
        "/api/v1/permissions", json={"identity": "etl", "catalog": "staging", "privileges": []}
    )

    assert refused.status_code == 422


async def test_an_unknown_field_is_refused(client: AsyncClient) -> None:
    refused = await client.post(
        "/api/v1/permissions",
        json={"identity": "etl", "catalog": "staging", "privileges": ["SELECT"], "columns": ["x"]},
    )

    assert refused.status_code == 422


async def test_granting_the_same_place_twice_is_a_conflict(client: AsyncClient) -> None:
    """Both rules would be in the file and first match wins, so the second would be dead
    weight an Operator could edit forever with no effect."""
    await client.post("/api/v1/permissions", json=READ_NATION)

    refused = await client.post(
        "/api/v1/permissions", json={**READ_NATION, "privileges": ["INSERT"]}
    )

    assert refused.status_code == 409
    assert "acme_finance:iceberg:finance:transactions" in refused.json()["message"]


async def test_what_a_grant_allows_can_be_changed(client: AsyncClient) -> None:
    await client.post("/api/v1/permissions", json=READ_NATION)

    updated = await client.put(
        "/api/v1/permissions/acme_finance:iceberg:finance:transactions",
        json={"privileges": ["SELECT", "INSERT"]},
    )

    assert updated.status_code == 200
    assert updated.json()["privileges"] == ["SELECT", "INSERT"]


async def test_a_grant_is_removed(client: AsyncClient) -> None:
    await client.post("/api/v1/permissions", json=READ_NATION)

    removed = await client.delete("/api/v1/permissions/acme_finance:iceberg:finance:transactions")

    assert removed.status_code == 204
    assert (await client.get("/api/v1/permissions")).json() == []


async def test_an_unknown_grant_is_not_found(client: AsyncClient) -> None:
    missing = await client.get("/api/v1/permissions/nobody:nothing:*:*")

    assert missing.status_code == 404


async def test_the_system_owned_rules_are_readable(client: AsyncClient) -> None:
    """An Operator who cannot see these cannot understand why catalog DDL is refused to
    them, or why a grant does not narrow anyone's access yet."""
    system = (await client.get("/api/v1/permissions/system")).json()

    assert len(system["rules"]) == 3
    assert all(rule["rule"] and rule["why"] for rule in system["rules"])


async def test_the_system_owned_rules_cannot_be_written(client: AsyncClient) -> None:
    refused = await client.put("/api/v1/permissions/system", json={"rules": []})

    assert refused.status_code == 409
    assert "generated" in refused.json()["message"]


async def test_grants_appear_in_review_by_key_and_cost_no_restart(client: AsyncClient) -> None:
    """Trino re-reads the rules on its own timer, so this is the first change an Operator
    can make to a busy Cluster without destroying every running query."""
    await client.post("/api/v1/permissions", json=READ_NATION)

    review = (await client.get("/api/v1/review")).json()

    permissions = next(s for s in review["sections"] if s["section"] == "permissions")
    assert [(c["resource"], c["change"]) for c in permissions["changes"]] == [
        ("acme_finance:iceberg:finance:transactions", "added")
    ]
    assert review["cost"]["restarts_coordinator"] is False
    assert review["cost"]["warning"] is None


async def test_reset_discards_staged_grants(client: AsyncClient) -> None:
    await client.post("/api/v1/permissions", json=READ_NATION)

    await client.post("/api/v1/candidate/reset")

    assert (await client.get("/api/v1/permissions")).json() == []


async def test_grant_mutations_are_refused_under_maintenance_mode(client: AsyncClient) -> None:
    await client.put(
        "/api/v1/admin/maintenance-mode", json={"engaged": True, "reason": "Trino upgrade"}
    )

    refused = await client.post("/api/v1/permissions", json=READ_NATION)

    assert refused.status_code == 409


def test_the_names_an_operator_writes_are_anchored() -> None:
    """Trino matches these as regular expressions. Unanchored, a grant on `finance` would
    also be a grant on `finance_archive` -- one nobody wrote and nobody would notice."""
    rules = json.loads(render_rules("apchi", {"k": {**READ_NATION, "schema": "finance"}}, "system"))

    granted = rules["tables"][1]
    assert granted["user"] == "^acme_finance$"
    assert granted["catalog"] == "^iceberg$"
    assert granted["schema"] == "^finance$"
    assert granted["table"] == "^transactions$"


def test_apchi_keeps_its_own_read_access() -> None:
    """A tables block denies every table it does not match, to everyone -- verified against
    a running coordinator, where apchi was refused a table it owned the catalog of."""
    rules = json.loads(render_rules("apchi", {}, "system"))

    assert rules["tables"][0] == {
        "user": "^apchi$",
        "catalog": "^system$",
        "privileges": ["SELECT"],
    }


def test_the_catch_all_table_rule_keeps_todays_posture() -> None:
    """Staging a grant records intent; it does not revoke anyone's access. Narrowing that is
    a decision of its own."""
    rules = json.loads(render_rules("apchi", {"k": READ_NATION}, "system"))

    assert rules["tables"][-1] == {
        "privileges": ["SELECT", "INSERT", "UPDATE", "DELETE", "OWNERSHIP", "GRANT_SELECT"]
    }

"""What Apchi will accept for a curated connector or Event Listener type, read back.

The frontend was hard-coding seven connectors' required, optional and branch rules because
nothing exposed them, which made a form a second copy of the validator that drifts silently
(#218, item 5). These tests pin the two properties that make the endpoint worth having: the
response is exhaustive over the schema, and what it says is what the write endpoints actually
enforce -- asserted by building a payload from the response and posting it.
"""

from httpx import AsyncClient

from app.sections.catalogs.schemas import CURATED_SCHEMAS as CONNECTORS
from app.sections.event_listeners.schemas import CURATED_SCHEMAS as LISTENER_TYPES
from app.sections.properties import SCHEMA_FIELDS, PropertyRules

CONNECTOR_RULES = "/api/v1/catalogs/connectors"
#: The Admin's, since the listener moved into the Server Configuration (#269).
LISTENER_RULES = "/api/v1/admin/server-configuration/event-listener-types"


async def test_every_curated_connector_is_listed(client: AsyncClient) -> None:
    response = await client.get(CONNECTOR_RULES)

    assert response.status_code == 200, response.text
    assert [rules["name"] for rules in response.json()] == sorted(CONNECTORS)


async def test_every_curated_listener_type_is_listed(client: AsyncClient) -> None:
    """Two against seven, and the asymmetry is real: a Cluster has one event listener
    configuration and many Catalogs."""
    response = await client.get(LISTENER_RULES)

    assert [rules["name"] for rules in response.json()] == sorted(LISTENER_TYPES)


async def test_the_response_covers_every_field_of_the_schema(client: AsyncClient) -> None:
    """The drift guard, and the reason this file exists.

    `PropertySchema` is what the write endpoints validate against. A field added to it and
    not to `describe` is a rule Apchi enforces and no client can discover -- which is the
    failure the hard-coded frontend copy already demonstrated. Derived from the dataclass
    rather than listed, so it fails on the day a field is added.
    """
    exposed = set(PropertyRules.model_fields) - {"name"}

    assert exposed == set(SCHEMA_FIELDS), (
        f"schema fields not in the response: {set(SCHEMA_FIELDS) - exposed}; "
        f"response fields not on the schema: {exposed - set(SCHEMA_FIELDS)}"
    )


async def test_a_branch_reports_what_each_value_requires(client: AsyncClient) -> None:
    """Iceberg is the case a flat list cannot express: six catalog types, each bringing its
    own required properties, one of them bringing none."""
    [iceberg] = [r for r in (await client.get(CONNECTOR_RULES)).json() if r["name"] == "iceberg"]

    [branch] = iceberg["branches"]
    assert branch["selector"] == "iceberg.catalog.type"
    assert branch["default"] == "hive_metastore"
    assert branch["allowed"] == sorted(branch["values"])
    assert branch["values"]["rest"] == ["iceberg.rest-catalog.uri"]
    assert branch["values"]["glue"] == [], "a branch that requires nothing further"


async def test_an_unconstrained_selector_reports_null_rather_than_an_empty_list(
    client: AsyncClient,
) -> None:
    """Inside the validator an empty `allowed` means "any value"; an empty list in JSON reads
    as "no value is allowed", which is the opposite. Nothing curated is unconstrained today,
    so this asserts the mapping instead of hoping a schema demonstrates it."""
    from app.sections.properties import Branch, PropertySchema, describe

    rules = describe("made-up", PropertySchema(branches=(Branch(selector="s", values={}),)))

    assert rules.branches[0].allowed is None


async def test_secrets_are_marked_where_the_name_does_not_say_so(client: AsyncClient) -> None:
    """The case that makes marking better than guessing. `mongodb.connection-url` embeds the
    password and the connector has no separate one, so a name test misses it -- and
    `s3.aws-access-key` reads like a secret and is an identifier that travels in every signed
    request, so a name test over-reports."""
    rules = {r["name"]: r for r in (await client.get(CONNECTOR_RULES)).json()}

    assert "mongodb.connection-url" in rules["mongodb"]["secrets"]
    assert "s3.aws-secret-key" in rules["hive"]["secrets"]
    assert "s3.aws-access-key" not in rules["hive"]["secrets"]
    assert "connection-password" in rules["postgresql"]["secrets"]
    assert "connection-user" not in rules["postgresql"]["secrets"]


async def test_a_secret_named_in_a_branch_is_still_reported(client: AsyncClient) -> None:
    """Elasticsearch's password arrives through the PASSWORD branch rather than `optional`, so
    a client reading only the flat lists would not find it. One set over the whole schema is
    why."""
    rules = {r["name"]: r for r in (await client.get(CONNECTOR_RULES)).json()}

    elastic = rules["elasticsearch"]
    assert "elasticsearch.auth.password" in elastic["secrets"]
    assert "elasticsearch.auth.password" not in elastic["optional"]
    assert "elasticsearch.auth.password" in elastic["branches"][0]["values"]["PASSWORD"]


async def test_every_marked_secret_is_a_property_the_connector_accepts(
    client: AsyncClient,
) -> None:
    """A secret marked under a name nothing accepts would be a field a form renders and every
    write refuses."""
    for rules in (await client.get(CONNECTOR_RULES)).json():
        accepted = set(CONNECTORS[rules["name"]].known())
        assert set(rules["secrets"]) <= accepted, rules["name"]


async def test_the_rules_are_the_rules_the_write_endpoint_enforces(
    client: AsyncClient,
) -> None:
    """The claim the endpoint makes, tested end to end rather than asserted.

    A Catalog built from nothing but the response -- every required property, and the ones
    the default branch brings in -- is accepted. Omitting one of them is refused, naming that
    property. If these two ever disagree, a form built from this endpoint is wrong.
    """
    rules = {r["name"]: r for r in (await client.get(CONNECTOR_RULES)).json()}["kafka"]
    [branch] = rules["branches"]
    required = rules["required"] + branch["values"][branch["default"]]
    assert sorted(required) == ["kafka.nodes", "kafka.table-names"]

    accepted = await client.post(
        "/api/v1/catalogs",
        json={
            "name": "events",
            "connector": "kafka",
            "properties": {name: "x" for name in required},
        },
    )
    assert accepted.status_code == 201, accepted.text

    refused = await client.post(
        "/api/v1/catalogs",
        json={
            "name": "events_two",
            "connector": "kafka",
            "properties": {"kafka.nodes": "x"},
        },
    )

    assert refused.status_code == 422
    assert "kafka.table-names" in refused.text


async def test_the_rules_do_not_need_a_mutation_to_be_allowed(client: AsyncClient) -> None:
    """Reference data, not configuration. It answers while Maintenance Mode is engaged,
    because a form that cannot load its own field list is a worse way to discover that
    editing is disabled than being told so when the edit is refused."""
    engaged = await client.put(
        "/api/v1/admin/maintenance-mode",
        json={"engaged": True, "reason": "Upgrading the platform."},
    )
    assert engaged.status_code == 200

    assert (await client.get(CONNECTOR_RULES)).status_code == 200
    assert (await client.get(LISTENER_RULES)).status_code == 200


async def test_a_catalog_cannot_be_called_connectors_by_accident(client: AsyncClient) -> None:
    """`/connectors` is declared before `/{name}`, so the path has to resolve to the rules.
    Worth pinning: the opposite order is a silent 404 on a working endpoint."""
    response = await client.get(CONNECTOR_RULES)

    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert response.json()[0]["name"] == "elasticsearch", "the rules, not a Catalog"

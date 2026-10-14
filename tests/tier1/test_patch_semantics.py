"""What a PATCH means, asserted rather than described (§19).

§19 requires PATCH semantics to be *"explicitly defined and consistent across every
endpoint"*. Taking that seriously found a defect: `certificate` could be set and never unset,
because `update.certificate or current.get(...)` cannot tell an omitted field from one sent
as null. Removing a client certificate from a Catalog meant dropping the Catalog and creating
it again — a `DROP CATALOG` in Trino, which is destructive and absurd for the purpose.

The semantics, which these tests pin:

* **named fields** — not JSON Merge Patch, not JSON Patch
* **omitted means unchanged**
* **present means replaces**, including `properties`, which is replaced *wholesale*
* **null means clear**, for the fields that can be cleared
* **the name is immutable** — renaming is a drop and a create

The last assertion in this file is the one §19 actually asked for: that the two PATCH
endpoints agree. Correct on each, separately, is not the same promise.
"""

from typing import Any

from httpx import AsyncClient

from tests.certificates import bundle

CATALOGS = "/api/v1/catalogs"
LISTENERS = "/api/v1/event-listeners"

#: TLS on the connection, because a client certificate does nothing without it and the
#: Section refuses the pair otherwise -- which is correct, and not what this file is about.
TLS_URL = "jdbc:postgresql://db.internal:5432/finance?sslmode=verify-full"


async def _catalog(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "finance_pg",
        "connector": "postgresql",
        "properties": {"connection-url": TLS_URL},
        **overrides,
    }
    response = await client.post(CATALOGS, json=body)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _with_certificate(client: AsyncClient) -> dict[str, Any]:
    uploaded = await client.post(
        "/api/v1/certificates",
        data={"name": "analytics"},
        files={"archive": ("analytics.zip", bundle(), "application/zip")},
    )
    assert uploaded.status_code == 201, uploaded.text
    return await _catalog(client, certificate="analytics")


# --- the defect ---------------------------------------------------------------------------


async def test_null_clears_a_certificate(client: AsyncClient) -> None:
    """The fix. Before this, `certificate` was set-once: the only way to remove one was to
    drop the Catalog and create it again, which in Trino is a DROP CATALOG."""
    await _with_certificate(client)

    response = await client.patch(f"{CATALOGS}/finance_pg", json={"certificate": None})

    assert response.status_code == 200, response.text
    assert response.json().get("certificate") is None


async def test_omitting_a_certificate_leaves_it_alone(client: AsyncClient) -> None:
    """The other half of the fix, and the half that makes it a fix rather than a swap: the
    distinction between omitted and null is the whole point."""
    await _with_certificate(client)

    response = await client.patch(
        f"{CATALOGS}/finance_pg", json={"properties": {"connection-url": TLS_URL}}
    )

    assert response.status_code == 200, response.text
    assert response.json()["certificate"] == "analytics"


# --- present means replaces ---------------------------------------------------------------


async def test_properties_are_replaced_not_merged(client: AsyncClient) -> None:
    """The sharp edge, pinned so it cannot drift into a merge later without somebody
    deciding to.

    A PATCH naming one property discards the others. That is the defensible of the two
    readings -- the usual result is a validation failure rather than a silently truncated
    Catalog -- but a client has to be told, which is why the contract now describes it.
    """
    await _catalog(
        client,
        properties={"connection-url": TLS_URL, "case-insensitive-name-matching": "true"},
    )

    response = await client.patch(
        f"{CATALOGS}/finance_pg", json={"properties": {"connection-url": TLS_URL}}
    )

    assert response.status_code == 200, response.text
    assert response.json()["properties"] == {"connection-url": TLS_URL}


async def test_a_patch_that_drops_a_required_property_is_refused(client: AsyncClient) -> None:
    """Because `properties` replaces, a PATCH that leaves out `connection-url` removes it --
    and that has to be a 422 naming the property rather than a stored Catalog that cannot
    start."""
    await _catalog(client)

    response = await client.patch(f"{CATALOGS}/finance_pg", json={"properties": {}})

    assert response.status_code == 422, response.text
    assert any("connection-url" in str(detail) for detail in response.json()["details"])


async def test_an_empty_body_changes_nothing(client: AsyncClient) -> None:
    """Not an error, and not a reset. A PATCH naming no fields asks for no change."""
    created = await _catalog(client)

    response = await client.patch(f"{CATALOGS}/finance_pg", json={})

    assert response.status_code == 200, response.text
    assert response.json() == created


# --- what cannot be cleared ----------------------------------------------------------------


async def test_a_connector_cannot_be_cleared(client: AsyncClient) -> None:
    """Null means clear, and some fields cannot be: a Catalog without a connector is not a
    Catalog. Refused by name rather than silently treated as "leave it alone", which is what
    made the certificate bug possible in the first place."""
    await _catalog(client)

    response = await client.patch(f"{CATALOGS}/finance_pg", json={"connector": None})

    assert response.status_code == 422, response.text
    assert response.json()["details"] == [{"property": "connector", "problem": "cannot be cleared"}]


async def test_properties_cannot_be_cleared(client: AsyncClient) -> None:
    await _catalog(client)

    response = await client.patch(f"{CATALOGS}/finance_pg", json={"properties": None})

    assert response.status_code == 422, response.text
    assert response.json()["details"] == [
        {"property": "properties", "problem": "cannot be cleared"}
    ]


async def test_the_name_is_immutable(client: AsyncClient) -> None:
    """Renaming is a drop and a create, which is a different and more destructive operation.
    The model forbids the field rather than ignoring it, so a client that tries learns."""
    await _catalog(client)

    response = await client.patch(f"{CATALOGS}/finance_pg", json={"name": "something_else"})

    assert response.status_code == 422, response.text


# --- the consistency §19 asked for --------------------------------------------------------


async def test_both_patch_endpoints_agree(client: AsyncClient) -> None:
    """§19 asks for semantics *consistent across every endpoint*, which is a different and
    weaker-to-forget promise than each one being correct on its own.

    Event Listeners have no clearable field, so what is checked is the shape of the rules:
    omitted leaves alone, present replaces wholesale, null on an unclearable field is refused
    the same way with the same detail.
    """
    created = await client.post(
        LISTENERS,
        json={
            "name": "audit_http",
            "type": "http",
            "properties": {"http-event-listener.connect-ingest-uri": "http://sink.internal"},
        },
    )
    assert created.status_code == 201, created.text

    unchanged = await client.patch(f"{LISTENERS}/audit_http", json={})
    refused = await client.patch(f"{LISTENERS}/audit_http", json={"type": None})
    cleared = await client.patch(f"{LISTENERS}/audit_http", json={"properties": None})

    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json() == created.json()
    assert refused.status_code == 422, refused.text
    assert refused.json()["details"] == [{"property": "type", "problem": "cannot be cleared"}]
    assert cleared.json()["details"] == [{"property": "properties", "problem": "cannot be cleared"}]

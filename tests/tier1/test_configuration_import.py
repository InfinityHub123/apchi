"""Importing a configuration, at both scopes.

An import replaces. That is why it is a PUT, and it is the source of everything that has to
be got right here: it discards staged work, it is rejected whole rather than one problem at
a time, and at the Admin scope it replaces Admin values the document does not mention.

The Candidate is shared (ADR-0002), so an invalid resource reaching it would block
everyone's Apply rather than only the importer's. Nothing is stored until all of it is
acceptable.
"""

import asyncio

from httpx import AsyncClient

from app.pipeline.applies import TERMINAL
from app.pipeline.configuration import FORMAT
from app.sections.registry import SECTIONS

CANDIDATE = "/api/v1/candidate"
CANDIDATE_EXPORT = "/api/v1/candidate/export"
ADMIN_CONFIGURATION = "/api/v1/admin/configuration"
ADMIN_EXPORT = "/api/v1/admin/configuration/export"

CATALOG = {"name": "finance", "connector": "memory", "properties": {}}


def _document(**sections: dict) -> dict:
    """A document with every Section present, which is what an import requires."""
    return {
        "format": FORMAT,
        "sections": {name: sections.get(name, {}) for name in SECTIONS},
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


# --- the round trip, which is the whole feature ------------------------------------------


async def test_an_exported_document_imports_unedited(client: AsyncClient) -> None:
    """The feature in one test. Provenance comes back in the body and has to be accepted
    on the way in, or an Operator would have to hand-edit every file they exported."""
    assert (await client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201
    exported = (await client.get(CANDIDATE_EXPORT)).json()
    assert (await client.delete("/api/v1/catalogs/finance")).status_code == 204

    response = await client.put(CANDIDATE, json=exported)

    assert response.status_code == 200, response.json()
    assert set((await client.get(CANDIDATE_EXPORT)).json()["sections"]["catalogs"]) == {"finance"}


async def test_an_import_replaces_rather_than_merges(client: AsyncClient) -> None:
    """PUT means replace. A catalog staged before the import is gone after it, which is
    what makes a document a configuration rather than a patch."""
    assert (await client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201

    response = await client.put(
        CANDIDATE, json=_document(catalogs={"other": {"connector": "memory", "properties": {}}})
    )

    assert response.status_code == 200, response.json()
    assert set((await client.get(CANDIDATE_EXPORT)).json()["sections"]["catalogs"]) == {"other"}


async def test_the_response_is_the_review_of_what_an_apply_would_now_change(
    client: AsyncClient,
) -> None:
    """An import is expected to produce a large diff -- unlike a Reset, which always
    produces an empty one -- so the response says what it is."""
    response = await client.put(CANDIDATE, json=_document(catalogs={"finance": CATALOG | {}}))

    assert response.status_code == 200, response.json()
    review = response.json()
    assert review["has_changes"] is True
    catalogs = next(s for s in review["sections"] if s["section"] == "catalogs")
    assert [c["resource"] for c in catalogs["changes"]] == ["finance"]


async def test_the_base_snapshot_is_this_clusters_latest_not_the_documents(
    applying_client: AsyncClient,
) -> None:
    """Review has to keep meaning "what would applying this change *here*".

    A base taken from the document would diff an imported configuration against a Cluster
    this one is not, and report a change nobody is about to make.
    """
    assert (await applying_client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201
    number = await _apply(applying_client)
    foreign = _document(catalogs={"finance": CATALOG | {}})
    foreign["provenance"] = {
        "exported_at": "2020-01-01T00:00:00Z",
        "apchi_version": "0.0.1",
        "source": "snapshot",
        "snapshot": 999,
    }

    response = await applying_client.put(CANDIDATE, json=foreign)

    assert response.status_code == 200, response.json()
    assert response.json()["base_snapshot"] == number


# --- refusals ----------------------------------------------------------------------------


async def test_a_document_that_is_not_one_of_these_is_refused(client: AsyncClient) -> None:
    """What `format` is for: refusing a file outright rather than discovering halfway in."""
    response = await client.put(
        CANDIDATE, json={"format": "something.else/v9", "sections": {name: {} for name in SECTIONS}}
    )

    assert response.status_code == 422, response.json()


async def test_an_unknown_section_is_named_rather_than_recited_at(client: AsyncClient) -> None:
    """The raw literal error lists all six valid names and does not say which key was
    wrong. An Operator editing a document needs the key they got wrong."""
    document = _document()
    document["sections"]["query_logging"] = {}

    response = await client.put(CANDIDATE, json=document)

    assert response.status_code == 422, response.json()
    problems = " ".join(d["problem"] for d in response.json()["details"])
    assert "query_logging" in problems


async def test_a_missing_section_is_refused_rather_than_emptied(client: AsyncClient) -> None:
    """A replacement says what the configuration *is*, so an omitted Section is ambiguous
    between "empty it" and "I trimmed the file". Emptying five Sections because a document
    mentioned one is not a thing to do silently."""
    assert (await client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201

    response = await client.put(CANDIDATE, json={"format": FORMAT, "sections": {"catalogs": {}}})

    assert response.status_code == 422, response.json()
    sections = {d["section"] for d in response.json()["details"]}
    assert sections == set(SECTIONS) - {"catalogs"}
    assert set((await client.get(CANDIDATE_EXPORT)).json()["sections"]["catalogs"]) == {"finance"}


async def test_every_problem_is_reported_not_just_the_first(client: AsyncClient) -> None:
    """An Operator fixing a file one rejection at a time is what importing a whole
    configuration exists to avoid."""
    response = await client.put(
        CANDIDATE,
        json=_document(
            catalogs={"no_connector": {"properties": {}}, "not_an_object": "a string"},
            event_listeners={"empty": {}},
        ),
    )

    assert response.status_code == 422, response.json()
    resources = {d["resource"] for d in response.json()["details"]}
    assert resources == {"no_connector", "not_an_object", "empty"}


async def test_nothing_is_stored_when_anything_is_wrong(client: AsyncClient) -> None:
    """The Candidate is shared, so one invalid resource in it blocks everyone's Apply and
    not just the importer's (ADR-0002). A partial import is the thing to avoid."""
    assert (await client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201

    response = await client.put(
        CANDIDATE,
        json=_document(catalogs={"good": {"connector": "memory", "properties": {}}, "bad": {}}),
    )

    assert response.status_code == 422, response.json()
    staged = (await client.get(CANDIDATE_EXPORT)).json()["sections"]["catalogs"]
    assert set(staged) == {"finance"}, "the pre-import Candidate must be untouched"


async def test_an_operator_cannot_import_admin_values(client: AsyncClient) -> None:
    """Refused rather than imported with the Admin values dropped. An Operator cannot set
    them and would have no way to know they had been lost."""
    document = _document()
    document["admin"] = {"enforce_permissions": True}

    response = await client.put(CANDIDATE, json=document)

    assert response.status_code == 422, response.json()
    assert (await client.get("/api/v1/admin/permissions/enforcement")).json()["enforced"] is False


# --- the Admin scope ---------------------------------------------------------------------


async def test_the_admin_import_replaces_both_halves(client: AsyncClient) -> None:
    document = _document(catalogs={"finance": CATALOG | {}})
    document["admin"] = {"enforce_permissions": True}

    response = await client.put(ADMIN_CONFIGURATION, json=document)

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["admin"]["enforce_permissions"] is True
    assert set(body["sections"]["catalogs"]) == {"finance"}


async def test_the_admin_import_replaces_admin_values_it_does_not_mention(
    client: AsyncClient,
) -> None:
    """Documented as intended, and the response says what the values now are.

    This is the shape of the bug #119 fixed on the preserved patterns, where a fresh
    AdminValues silently reset enforcement. Here the reset *is* the semantics of PUT, so
    what matters is that a caller is told rather than left to assume only what they sent
    changed.
    """
    assert (
        await client.put("/api/v1/admin/permissions/enforcement", json={"enforced": True})
    ).status_code == 200
    document = _document()
    document["admin"] = {"preserved_certificate_mappings": [{"pattern": "^legacy-(.*)$"}]}

    response = await client.put(ADMIN_CONFIGURATION, json=document)

    assert response.status_code == 200, response.json()
    assert response.json()["admin"]["enforce_permissions"] is False, (
        "an omitted value takes its default, which is what replacing means"
    )
    assert (await client.get("/api/v1/admin/permissions/enforcement")).json()["enforced"] is False


async def test_the_admin_import_requires_the_admin_block(client: AsyncClient) -> None:
    """Required rather than defaulted: an Operator document PUT here would otherwise turn
    enforcement off and drop an adopted Cluster's preserved rules through an omission."""
    response = await client.put(ADMIN_CONFIGURATION, json=_document())

    assert response.status_code == 422, response.json()


async def test_an_admin_import_validates_the_preserved_patterns(client: AsyncClient) -> None:
    """The same validation PUT /admin/certificate-mapping/preserved applies. A pattern that
    is not a usable regex would be written into the file Trino authenticates against."""
    document = _document()
    document["admin"] = {"preserved_certificate_mappings": [{"pattern": "([unclosed"}]}

    response = await client.put(ADMIN_CONFIGURATION, json=document)

    assert response.status_code == 422, response.json()


async def test_an_admin_export_round_trips_through_the_admin_import(client: AsyncClient) -> None:
    assert (
        await client.put("/api/v1/admin/permissions/enforcement", json={"enforced": True})
    ).status_code == 200
    assert (await client.post("/api/v1/catalogs", json=CATALOG)).status_code == 201
    exported = (await client.get(ADMIN_EXPORT)).json()

    response = await client.put(ADMIN_CONFIGURATION, json=exported)

    assert response.status_code == 200, response.json()
    assert response.json()["admin"]["enforce_permissions"] is True
    assert set(response.json()["sections"]["catalogs"]) == {"finance"}

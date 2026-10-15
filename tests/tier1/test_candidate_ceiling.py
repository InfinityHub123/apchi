"""The Candidate is one document, and one document has a ceiling.

ADR-0002 made the Configuration Candidate a single shared document, and §4 keeps it that way.
MongoDB caps a document at 16MB. Nothing capped the *number* of catalogs, rules or
certificates -- only the length of individual fields -- so a Cluster that accumulated enough
configuration used to meet `replace_one` failing inside a routine edit, reported as a 500 on
whichever edit happened to cross the line.

Three properties matter here, and the third is the one that would be easy to get wrong:

* a refusal rather than a crash, naming what grew
* the stored Candidate untouched, so the Operator is where they were and not somewhere worse
* **an import refused whole**, because that is the path by which a Cluster that was fine gets
  a configuration that does not fit
"""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.config import Settings
from app.main import create_app
from tests.conftest import FakeKubernetes

CATALOGS = "/api/v1/catalogs"
#: Small enough that a handful of catalogs crosses it, so these tests stay fast. The
#: production default is 15MB; what is under test is the guard, not the number.
CEILING = 40_000


def _properties(padding: int = 0) -> dict[str, str]:
    """A valid postgresql catalog, optionally a large one.

    Padded inside `connection-url` rather than with an extra property: static validation
    rejects a property the connector does not define, which is the right behaviour and not
    what these tests are about.
    """
    url = "jdbc:postgresql://db.internal:5432/finance"
    if padding:
        url = f"{url}?ApplicationName={'x' * padding}"
    return {"connection-url": url}


@pytest.fixture
async def tight(settings: Settings, fake_kubernetes: FakeKubernetes) -> AsyncIterator[AsyncClient]:
    """An Apchi whose Candidate ceiling is low enough to reach in a test."""
    app = create_app(settings.model_copy(update={"candidate_max_bytes": CEILING}))
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=1)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


async def _fill(http: AsyncClient, count: int, padding: int = 8_000) -> int:
    """Add catalogs until one is refused, returning how many landed."""
    for index in range(count):
        response = await http.post(
            CATALOGS,
            json={
                "name": f"filler_{index}",
                "connector": "postgresql",
                "properties": _properties(padding),
            },
        )
        if response.status_code != 201:
            return index
    return count


# --- the refusal --------------------------------------------------------------------------


async def test_an_edit_past_the_ceiling_is_refused_not_a_crash(tight: AsyncClient) -> None:
    """422 with a machine-readable code, not the 500 an unguarded `replace_one` produced."""
    landed = await _fill(tight, 20)
    assert 0 < landed < 20, f"the ceiling was never reached ({landed} catalogs landed)"

    response = await tight.post(
        CATALOGS,
        json={"name": "one_too_many", "connector": "postgresql", "properties": _properties(8_000)},
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "candidate_too_large"


async def test_the_refusal_names_what_grew(tight: AsyncClient) -> None:
    """ "Your configuration is too big" is not actionable. "catalogs is the largest" is."""
    await _fill(tight, 20)

    response = await tight.post(
        CATALOGS,
        json={"name": "one_too_many", "connector": "postgresql", "properties": _properties(8_000)},
    )

    body = response.json()
    assert "catalogs" in body["message"]
    assert body["details"] == [
        {"section": "catalogs", "problem": "the Candidate is at its size limit"}
    ]


async def test_the_message_explains_why_there_is_a_ceiling(tight: AsyncClient) -> None:
    """It is a consequence of the one-document design, not a policy somebody chose -- and an
    Operator who does not know that will think it is a quota they can ask to have raised."""
    await _fill(tight, 20)

    response = await tight.post(
        CATALOGS,
        json={"name": "one_too_many", "connector": "postgresql", "properties": _properties(8_000)},
    )

    assert "MongoDB" in response.json()["message"]
    assert "16 MB" in response.json()["message"]


# --- nothing is lost ----------------------------------------------------------------------


async def test_a_refused_edit_leaves_the_stored_candidate_untouched(tight: AsyncClient) -> None:
    """The property that makes this a refusal rather than a failure: the Operator is where
    they were, not somewhere worse."""
    landed = await _fill(tight, 20)
    before = (await tight.get(CATALOGS)).json()

    await tight.post(
        CATALOGS,
        json={"name": "one_too_many", "connector": "postgresql", "properties": _properties(8_000)},
    )

    after = (await tight.get(CATALOGS)).json()
    assert len(after) == landed
    assert after == before


async def test_removing_something_makes_room_again(tight: AsyncClient) -> None:
    """The refusal is about the total, so the remedy is removing something -- which is what
    the message tells the Operator to do, and it has to actually work."""
    landed = await _fill(tight, 20)

    assert (await tight.delete(f"{CATALOGS}/filler_0")).status_code == 204
    response = await tight.post(
        CATALOGS,
        json={"name": "room_now", "connector": "postgresql", "properties": _properties(8_000)},
    )

    assert response.status_code == 201, response.text
    assert len((await tight.get(CATALOGS)).json()) == landed


# --- imports, which is how a fine Cluster gets a configuration that does not fit ----------


async def test_an_oversized_import_is_refused_whole(tight: AsyncClient) -> None:
    """An import replaces the entire Candidate, so it is the path by which somebody else's
    larger configuration arrives at a Cluster that was fine. Refused before anything is
    stored, which is already how an import behaves about validation (#121)."""
    await tight.post(
        CATALOGS,
        json={"name": "mine", "connector": "postgresql", "properties": _properties()},
    )
    before = (await tight.get(CATALOGS)).json()

    response = await tight.put(
        "/api/v1/candidate",
        json={
            "format": "apchi.configuration/v1",
            "sections": {
                "catalogs": {
                    f"theirs_{i}": {
                        "name": f"theirs_{i}",
                        "connector": "postgresql",
                        "properties": _properties(8_000),
                    }
                    for i in range(20)
                },
                "client_certificates": {},
                "certificate_mapping": {},
                "permissions": {},
                "resource_groups": {},
                "event_listeners": {},
            },
            "admin": None,
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "candidate_too_large"
    assert (await tight.get(CATALOGS)).json() == before, "a refused import changed the Candidate"


# --- the default is generous --------------------------------------------------------------


async def test_an_ordinary_cluster_is_unaffected(client: AsyncClient) -> None:
    """The production default is 15MB. Nothing about a normal Candidate goes near it, and the
    guard must not cost an ordinary edit anything."""
    for index in range(5):
        response = await client.post(
            CATALOGS,
            json={
                "name": f"normal_{index}",
                "connector": "postgresql",
                "properties": _properties(),
            },
        )
        assert response.status_code == 201, response.text

    assert len((await client.get(CATALOGS)).json()) == 5


def test_the_ceiling_leaves_headroom_under_mongodbs_limit() -> None:
    """BSON overhead means the serialised size is not the document size, and a guard that only
    just fits is a guard that sometimes does not. Also leaves room for the Snapshot an Apply
    makes of the same content."""
    from app.pipeline.candidate import BSON_DOCUMENT_LIMIT

    assert Settings().candidate_max_bytes < BSON_DOCUMENT_LIMIT

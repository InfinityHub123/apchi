"""One pagination model, asserted per collection rather than once (§19).

§19 asks for *"one consistent pagination, filtering and sorting model"*. The thing worth
testing is not that a page comes back -- it is that the boundary holds, and that it holds for
each collection separately: each one has its own sort key, and two of those keys are not
unique, so each has its own chance to repeat or skip a row.

The Section collections are deliberately **not** paginated, and there is a test saying so with
the reason in it, because an omission and a decision look identical from outside.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.api.deps import caller
from app.api.pages import MAX_LIMIT, encode_cursor
from app.auth.identity import Identity
from app.config import AuthMode, Settings
from app.main import create_app
from tests.conftest import FakeKubernetes

SNAPSHOTS = "/api/v1/snapshots"
APPLIES = "/api/v1/applies"
TOKENS = "/api/v1/tokens"
AUDIT = "/api/v1/audit"
JO = Identity(subject="jo", role="admin", via="password")

#: Every paginated collection, and how to put rows into it. Parametrised rather than written
#: out five times, because the properties being asserted are the ones that must not differ
#: between them -- which is the whole of what §19 asked for.
PAGINATED = ("snapshots", "applies", "tokens", "audit")


@pytest.fixture
async def http(settings: Settings, fake_kubernetes: FakeKubernetes) -> AsyncIterator[AsyncClient]:
    app = create_app(settings.model_copy(update={"auth_mode": AuthMode.PASSWORD}))
    app.dependency_overrides[caller] = lambda: JO
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=1)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as client,
        app.router.lifespan_context(app),
    ):
        yield client


async def _seed(client: AsyncClient, collection: str, count: int, tie: bool = True) -> None:
    """Put `count` rows into a collection, through the store so the test stays fast.

    With `tie` -- the default -- pairs of rows share a timestamp, so the cursor's tiebreak has
    something to tie: two Applies starting in the same instant is the ordinary case, not a
    contrived one, and a cursor carrying only the sort key would lose one of them.

    Ordering tests pass `tie=False`. A tie makes the order *between* those two rows arbitrary
    -- the tiebreak is a random id -- so asserting a total order over tied rows is a test that
    passes on the luck of a uuid. It did, locally, and CI found it.
    """
    app = client._transport.app  # type: ignore[attr-defined]
    now = datetime.now(UTC)
    if collection == "snapshots":
        for index in range(count):
            await app.state.snapshot_store.commit({}, f"apl_{index}")
    elif collection == "applies":
        for index in range(count):
            record = await app.state.apply_store.create(base_snapshot=None)
            await app.state.apply_store._collection.update_one(
                {"_id": record.id},
                {"$set": {"started_at": now - _offset(count, index, tie)}},
            )
    elif collection == "tokens":
        for index in range(count):
            minted = await app.state.token_store.create("jo", f"token_{index}")
            await app.state.token_store._collection.update_one(
                {"_id": minted.token.id},
                {"$set": {"created_at": now - _offset(count, index, tie)}},
            )
    elif collection == "audit":
        for index in range(count):
            await app.state.audit_store.record(
                "jo", "password", "catalogs", "created", resource=f"c_{index}"
            )


def _offset(count: int, index: int, tie: bool) -> timedelta:
    """How far back a row sits. Paired when ties are wanted, distinct when they are not."""
    steps = count - 1 - index
    return timedelta(seconds=steps // 2 if tie else steps)


def _url(collection: str) -> str:
    return f"/api/v1/{collection}"


# --- the envelope is the same everywhere --------------------------------------------------


@pytest.mark.parametrize("collection", PAGINATED)
async def test_every_collection_answers_the_same_envelope(
    http: AsyncClient, collection: str
) -> None:
    """`items` and `next`, with the same names everywhere. §19's "one consistent model" is
    mostly this: a client that can page one collection can page all of them."""
    await _seed(http, collection, 3)

    body = (await http.get(_url(collection), params={"limit": 2})).json()

    assert set(body) == {"items", "next"}
    assert len(body["items"]) == 2
    assert body["next"]


@pytest.mark.parametrize("collection", PAGINATED)
async def test_the_last_page_has_no_cursor(http: AsyncClient, collection: str) -> None:
    """So a client can tell "that was everything" from "ask again"."""
    await _seed(http, collection, 2)

    body = (await http.get(_url(collection), params={"limit": 50})).json()

    assert len(body["items"]) == 2
    assert body["next"] is None


@pytest.mark.parametrize("collection", PAGINATED)
async def test_a_boundary_neither_repeats_nor_skips(http: AsyncClient, collection: str) -> None:
    """Walked to the end and compared against the whole set.

    Per collection, because each has its own sort key and two of them are not unique -- the
    seeding deliberately gives pairs of rows the same timestamp, so a cursor carrying only the
    sort key would lose one here.
    """
    await _seed(http, collection, 9)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(12):
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["before"] = cursor
        body = (await http.get(_url(collection), params=params)).json()
        seen += [_identity_of(item) for item in body["items"]]
        cursor = body["next"]
        if cursor is None:
            break

    assert cursor is None, f"{collection} never reached the end"
    assert len(seen) == 9, f"{collection} returned {len(seen)} of 9"
    assert len(set(seen)) == 9, f"{collection} repeated a row"


@pytest.mark.parametrize("collection", PAGINATED)
async def test_a_mangled_cursor_is_refused_the_same_way(http: AsyncClient, collection: str) -> None:
    """One refusal for the same mistake, whichever collection it is made against."""
    response = await http.get(_url(collection), params={"before": "not-a-cursor"})

    assert response.status_code == 422, response.text
    assert response.json()["details"] == [
        {"property": "before", "problem": "not a cursor this endpoint issued"}
    ]


@pytest.mark.parametrize("collection", PAGINATED)
async def test_a_cursor_from_another_collection_is_refused(
    http: AsyncClient, collection: str
) -> None:
    """They are opaque, but they are not interchangeable: a snapshot cursor is one number and
    the others are two parts, so handing one to the wrong endpoint must refuse rather than be
    half-read."""
    foreign = encode_cursor("one", "two", "three")

    response = await http.get(_url(collection), params={"before": foreign})

    assert response.status_code == 422, response.text


@pytest.mark.parametrize("collection", PAGINATED)
async def test_the_limit_is_bounded(http: AsyncClient, collection: str) -> None:
    """A limit a client can raise without bound is an unbounded response wearing a
    parameter."""
    response = await http.get(_url(collection), params={"limit": MAX_LIMIT + 1})

    assert response.status_code == 422, response.text


# --- newest first, per collection's own key -----------------------------------------------


async def test_snapshots_are_newest_first_by_number(http: AsyncClient) -> None:
    await _seed(http, "snapshots", 3)

    items = (await http.get(SNAPSHOTS)).json()["items"]

    assert [s["number"] for s in items] == [3, 2, 1]


async def test_tokens_are_newest_first(http: AsyncClient) -> None:
    """Reversed from the ascending order this used to answer in, so `before` means the same
    thing here as everywhere else: a cursor over an ascending sort pages backwards."""
    await _seed(http, "tokens", 3, tie=False)

    items = (await http.get(TOKENS)).json()["items"]

    assert [t["name"] for t in items] == ["token_2", "token_1", "token_0"]


# --- the collections that are deliberately whole ------------------------------------------


@pytest.mark.parametrize(
    "section",
    [
        "catalogs",
        "certificates",
        "permissions",
        "resource-groups",
        "event-listeners",
    ],
)
async def test_section_collections_answer_whole(http: AsyncClient, section: str) -> None:
    """Not paginated, deliberately, and this says so rather than leaving it to look like an
    omission.

    Five of the six Section collections live inside the single Candidate document (ADR-0002,
    §4), which is loaded whole on every read -- so slicing it afterwards saves nothing. And
    §21 needs every Section at once to show the full diff before an Apply; a paginated Section
    is a diff assembled from several round trips that can disagree with each other.

    Their real limit is the 16MB document ceiling, which §4 records and `CandidateStore`
    enforces (#149).
    """
    response = await http.get(f"/api/v1/{section}")

    assert response.status_code == 200, response.text
    assert isinstance(response.json(), list), (
        f"{section} answered an envelope. Section collections are whole by decision: they "
        "live in one document and §21 needs them together."
    )


def _identity_of(item: dict[str, Any]) -> str:
    """Whatever uniquely names a row in whichever collection it came from."""
    for key in ("id", "number", "name"):
        if key in item:
            return str(item[key])
    raise AssertionError(f"no identifying field in {item}")

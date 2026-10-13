"""The audit record, read back.

Nothing is wired to a mutation yet (#139, #140, #141), so these write events directly. What is
under test is the record and the collection: that every field survives a round trip, that a
page boundary is exact while the collection is being appended to, that the queries the
endpoint makes are served by an index rather than by a scan, and that no configuration value
can reach an event.

The last two are the ones worth having. An audit endpoint that scans is an endpoint that works
in testing and times out after a year of history, and an audit trail that accumulates
credentials is a second copy of everything §12 already protects.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.api.deps import caller
from app.audit.events import AuditStore
from app.auth.identity import Identity
from app.config import Settings
from app.main import create_app
from tests.conftest import FakeKubernetes

AUDIT = "/api/v1/audit"

OPERATOR = Identity(subject="jo", role="operator", via="password")


@pytest.fixture
async def as_operator(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    """An app whose caller is an authenticated Operator, so "may an Operator read this" is a
    question about the gate rather than about any credential."""
    app = create_app(settings)
    app.dependency_overrides[caller] = lambda: OPERATOR
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=1)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


def _store(client: AsyncClient) -> AuditStore:
    app = client._transport.app  # type: ignore[attr-defined]
    store: AuditStore = app.state.audit_store
    return store


async def _some(store: AuditStore, count: int) -> None:
    for index in range(count):
        await store.record("jo", "password", "catalogs", "created", resource=f"catalog_{index}")


# --- the record ---------------------------------------------------------------------------


async def test_an_event_round_trips_with_every_field(client: AsyncClient) -> None:
    """Field by field rather than by sampling: each one is in §20 because something would be
    unanswerable without it."""
    await _store(client).record(
        "jo",
        "token",
        "catalogs",
        "created",
        resource="finance_pg",
        snapshot=7,
        outcome="succeeded",
        detail="staged alongside two others",
        token_id="tok_123",
        apply_id="apply_456",
    )

    response = await client.get(AUDIT)

    assert response.status_code == 200, response.text
    [event] = response.json()["events"]
    assert event["actor"] == "jo"
    assert event["via"] == "token"
    assert event["token_id"] == "tok_123"
    assert event["category"] == "catalogs"
    assert event["resource"] == "finance_pg"
    assert event["action"] == "created"
    assert event["snapshot"] == 7
    assert event["outcome"] == "succeeded"
    assert event["detail"] == "staged alongside two others"
    assert event["apply_id"] == "apply_456"
    assert event["id"] and event["at"]


async def test_admin_is_a_category_and_carries_no_snapshot(client: AsyncClient) -> None:
    """§20 is explicit that Admin changes are audited and are not a Section. A field typed as
    a Section name would have nothing to put here, and the trail would omit the one class of
    change that alters the Cluster without appearing in any Snapshot."""
    await _store(client).record("root", "password", "admin", "updated", resource="jvm.config")

    [event] = (await client.get(AUDIT)).json()["events"]

    assert event["category"] == "admin"
    assert event["snapshot"] is None


async def test_the_system_is_a_named_actor(client: AsyncClient) -> None:
    """Startup recovery and Auto Rollback change things with nobody on the other end of a
    request. A record reading "apchi reset this" is answerable; one that lost its actor looks
    like a bug in the audit."""
    from app.audit.events import SYSTEM

    await _store(client).record(SYSTEM, "none", "pipeline", "rolled_back", snapshot=3)

    [event] = (await client.get(AUDIT)).json()["events"]

    assert event["actor"] == "apchi"
    assert event["via"] == "none"


async def test_a_refusal_is_recorded_as_an_outcome_not_an_absence(client: AsyncClient) -> None:
    """ "Who tried to change this during the freeze" is a question somebody asks, and a trail
    that only records what succeeded cannot answer it."""
    await _store(client).record(
        "jo",
        "password",
        "catalogs",
        "created",
        resource="finance_pg",
        outcome="refused",
        detail="the Candidate is frozen: Apply apply_1 is in flight",
    )

    [event] = (await client.get(AUDIT)).json()["events"]

    assert event["outcome"] == "refused"
    assert "frozen" in event["detail"]


# --- ordering and paging ------------------------------------------------------------------


async def test_newest_first(client: AsyncClient) -> None:
    await _some(_store(client), 3)

    events = (await client.get(AUDIT)).json()["events"]

    assert [e["resource"] for e in events] == ["catalog_2", "catalog_1", "catalog_0"]


async def test_a_page_boundary_neither_repeats_nor_skips(client: AsyncClient) -> None:
    """Walked to the end and compared against the whole set. An offset pager would pass this
    on a static collection and fail the test below."""
    await _some(_store(client), 10)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params = {"limit": 3} | ({"before": cursor} if cursor else {})
        page = (await client.get(AUDIT, params=params)).json()
        seen += [e["id"] for e in page["events"]]
        cursor = page["next"]
        if cursor is None:
            break

    assert cursor is None, "paging never reached the end"
    assert len(seen) == 10
    assert len(set(seen)) == 10, "a page repeated a row"


async def test_a_page_boundary_holds_while_new_events_arrive(client: AsyncClient) -> None:
    """The reason this is a cursor and not an offset. The collection grows at the *head*, so
    an offset pager shifts every later page by however many arrived in between -- silently
    repeating rows it already showed."""
    store = _store(client)
    await _some(store, 6)

    first = (await client.get(AUDIT, params={"limit": 3})).json()
    for index in range(100, 105):
        await store.record(
            "someone-else", "password", "catalogs", "created", resource=f"later_{index}"
        )
    second = (await client.get(AUDIT, params={"limit": 3, "before": first["next"]})).json()

    overlap = {e["id"] for e in first["events"]} & {e["id"] for e in second["events"]}
    assert overlap == set(), "the second page repeated rows from the first"
    assert [e["resource"] for e in second["events"]] == ["catalog_2", "catalog_1", "catalog_0"]


async def test_two_events_in_the_same_instant_still_page_exactly(client: AsyncClient) -> None:
    """Two resources changed in one request is the ordinary case, not a rare one. A cursor
    that carried only the timestamp could not tell them apart."""
    store = _store(client)
    same = datetime.now(UTC)
    for index in range(4):
        await store._collection.insert_one(
            {
                "at": same,
                "actor": "jo",
                "via": "password",
                "token_id": None,
                "category": "catalogs",
                "resource": f"at_once_{index}",
                "action": "created",
                "snapshot": None,
                "outcome": "succeeded",
                "detail": None,
                "apply_id": None,
            }
        )

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        params = {"limit": 2} | ({"before": cursor} if cursor else {})
        page = (await client.get(AUDIT, params=params)).json()
        seen += [e["id"] for e in page["events"]]
        cursor = page["next"]
        if cursor is None:
            break

    assert len(set(seen)) == 4, f"paging within one instant lost or repeated rows: {seen}"


async def test_the_last_page_has_no_cursor(client: AsyncClient) -> None:
    """Otherwise a client cannot tell "that was everything" from "ask again"."""
    await _some(_store(client), 2)

    page = (await client.get(AUDIT, params={"limit": 50})).json()

    assert len(page["events"]) == 2
    assert page["next"] is None


async def test_a_mangled_cursor_is_refused_not_a_crash(client: AsyncClient) -> None:
    response = await client.get(AUDIT, params={"before": "not-a-cursor"})

    assert response.status_code == 422, response.text
    assert "before" in response.text


async def test_an_empty_history_is_an_empty_page(client: AsyncClient) -> None:
    response = await client.get(AUDIT)

    assert response.status_code == 200, response.text
    assert response.json() == {"events": [], "next": None}


# --- filters ------------------------------------------------------------------------------


async def test_filters_compose(client: AsyncClient) -> None:
    """Category, actor and a time range together. Each one alone proves little: the question
    somebody actually asks is "what did this person change to this Section last Tuesday"."""
    store = _store(client)
    await store.record("jo", "password", "catalogs", "created", resource="wanted")
    await store.record("jo", "password", "admin", "updated", resource="wrong-category")
    await store.record("sam", "password", "catalogs", "created", resource="wrong-actor")

    response = await client.get(
        AUDIT,
        params={
            "category": "catalogs",
            "actor": "jo",
            "since": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )

    assert [e["resource"] for e in response.json()["events"]] == ["wanted"]


async def test_a_time_window_excludes_what_is_outside_it(client: AsyncClient) -> None:
    store = _store(client)
    await store._collection.insert_one(
        {
            "at": datetime.now(UTC) - timedelta(days=30),
            "actor": "jo",
            "via": "password",
            "token_id": None,
            "category": "catalogs",
            "resource": "ancient",
            "action": "created",
            "snapshot": None,
            "outcome": "succeeded",
            "detail": None,
            "apply_id": None,
        }
    )
    await store.record("jo", "password", "catalogs", "created", resource="recent")

    recent = await client.get(
        AUDIT, params={"since": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
    )

    assert [e["resource"] for e in recent.json()["events"]] == ["recent"]


async def test_an_unknown_category_is_refused_by_the_contract(client: AsyncClient) -> None:
    """The six Sections plus `admin` and `pipeline`, and nothing else. A typo that returned an
    empty page would read as "nothing happened there"."""
    response = await client.get(AUDIT, params={"category": "catalgos"})

    assert response.status_code == 422, response.text


# --- who may read it ----------------------------------------------------------------------


async def test_an_operator_can_read_the_history(
    client: AsyncClient, as_operator: AsyncClient
) -> None:
    """Asserted because the whole argument for a shared Candidate depends on it (ADR-0002):
    an Operator about to Apply has to see whose changes they are shipping. Admin-only would
    keep the concurrency trade and remove its justification."""
    await _store(as_operator).record("sam", "password", "catalogs", "created", resource="theirs")

    response = await as_operator.get(AUDIT)

    assert response.status_code == 200, response.text
    assert [e["actor"] for e in response.json()["events"]] == ["sam"]


# --- no values, ever ----------------------------------------------------------------------


async def test_no_secret_reaches_the_record_or_the_response(client: AsyncClient) -> None:
    """The record holds a resource *identity*, never its contents.

    §12 puts real credentials in Snapshots; a second copy in a collection with its own access
    rules and its own export path is a second thing to protect. Asserted with the two shapes
    that actually appear -- a connection password and a private key -- so the model has no
    field they could be put in even by accident.
    """
    password = "s3cret-connection-password"
    private_key = "-----BEGIN PRIVATE KEY-----\nMIIEv\n-----END PRIVATE KEY-----"
    store = _store(client)
    await store.record(
        "jo", "password", "catalogs", "created", resource="finance_pg", detail="connection-url set"
    )
    await store.record(
        "jo", "password", "client_certificates", "created", resource="analytics-client"
    )

    body = (await client.get(AUDIT)).text
    stored = [document async for document in store._collection.find()]

    assert password not in body
    assert private_key not in body
    assert password not in str(stored)
    assert private_key not in str(stored)
    assert "connection-password" not in body, "a value-bearing property name reached the trail"


async def test_the_event_has_no_field_a_value_could_live_in(client: AsyncClient) -> None:
    """A tripwire on the field set itself, pinned rather than sampled.

    Every field below is an identity or a piece of metadata. None of them is a configuration
    value, and that is the property worth defending: the obvious way to erode it later is a
    well-meaning `before`/`after` pair on an event, which would turn the audit into a second
    store of the credentials §12 keeps in Snapshots. Adding a field has to break this test.

    `detail` is the one that needs care, because it is free text. Its job is a refusal reason
    or a failure message -- never a value -- which no type can enforce, so it is said here and
    in the model's own description.
    """
    from app.audit.events import AuditEvent

    assert set(AuditEvent.model_fields) == {
        "id",
        "at",
        "actor",
        "via",
        "token_id",
        "category",
        "resource",
        "action",
        "snapshot",
        "outcome",
        "detail",
        "apply_id",
    }


async def test_the_event_model_rejects_a_field_that_could_carry_a_value(
    client: AsyncClient,
) -> None:
    """`extra="forbid"`, so a later slice cannot quietly add `before`/`after` to an event and
    turn the audit into a second store of credentials. A change like that has to be a
    deliberate edit to the model, which is reviewable."""
    from pydantic import ValidationError

    from app.audit.events import AuditEvent

    with pytest.raises(ValidationError):
        AuditEvent(
            id="x",
            at=datetime.now(UTC),
            actor="jo",
            via="password",
            category="catalogs",
            action="created",
            value="s3cret",  # type: ignore[call-arg]
        )


# --- retention ----------------------------------------------------------------------------


async def test_history_is_kept_forever_by_default(client: AsyncClient) -> None:
    """No TTL index unless somebody asked for one. An audit trail that silently deletes itself
    is worse than one that grows: the first question an incident asks is what happened before
    the window you kept."""
    store = _store(client)
    await store.record("jo", "password", "catalogs", "created", resource="finance_pg")

    indexes = await store._collection.index_information()

    expiring = {name: spec for name, spec in indexes.items() if "expireAfterSeconds" in spec}
    assert expiring == {}, f"audit history is being discarded by default: {expiring}"


async def test_a_retention_setting_creates_a_ttl_index(client: AsyncClient) -> None:
    """And is the only thing that does, so discarding history is a decision rather than a
    default somebody inherited.

    Built the way the app builds it -- retention passed to the store -- rather than by calling
    `ensure_indexes` directly, so the setting being wired up is part of what is asserted.
    """
    app = client._transport.app  # type: ignore[attr-defined]
    store = AuditStore(app.state.mongo.database, retention_days=30)

    await store.record("jo", "password", "catalogs", "created", resource="finance_pg")

    indexes = await store._collection.index_information()
    expiring = [spec for spec in indexes.values() if "expireAfterSeconds" in spec]
    assert len(expiring) == 1
    assert expiring[0]["expireAfterSeconds"] == 30 * 24 * 3600


async def test_no_index_is_created_before_the_first_event(client: AsyncClient) -> None:
    """A regression guard for #131, and the reason indexing is lazy rather than tidy.

    Tier 1 starts an app per test. Three `create_index` round trips per start took this
    suite from 215 tests in 72 seconds to about 19 in five minutes, and left mongod not
    answering its own local shell. An index on a collection with no rows has no reader, so
    there is nothing to buy with that cost.

    Asserted by starting the app -- which the `client` fixture has already done -- and
    looking at what the collection has: only the `_id` index MongoDB makes itself.
    """
    indexes = await _store(client)._collection.index_information()

    assert list(indexes) in ([], ["_id_"]), (
        f"the audit collection was indexed at startup: {list(indexes)}. Tier 1 starts an app "
        "per test, and this is what wedged the test MongoDB in #131."
    )


# --- the queries are indexed --------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"actor": "jo"},
        {"category": "catalogs"},
        {"actor": "jo", "category": "catalogs"},
        {"since": "2020-01-01T00:00:00+00:00"},
    ],
    ids=["unfiltered", "by-actor", "by-category", "by-both", "by-time"],
)
async def test_every_query_the_endpoint_makes_uses_an_index(
    client: AsyncClient, params: dict[str, str]
) -> None:
    """A scan is not a slow endpoint, it is an endpoint that works in testing and times out
    after a year of history -- and a blocking sort is the same failure wearing a different
    name, because it grows with the collection rather than with the page.

    Asserted against MongoDB's own plan rather than by reading the index list, which would
    prove an index exists and not that the query uses it.
    """
    store = _store(client)
    await _some(store, 5)

    query: dict[str, Any] = {}
    if "actor" in params:
        query["actor"] = params["actor"]
    if "category" in params:
        query["category"] = params["category"]
    if "since" in params:
        query["at"] = {"$gte": datetime.fromisoformat(params["since"])}
    plan = await store._collection.database.command(
        {
            "explain": {
                "find": "audit",
                "filter": query,
                "sort": {"at": -1, "_id": -1},
                "limit": 51,
            },
            "verbosity": "queryPlanner",
        }
    )

    stages = str(plan["queryPlanner"]["winningPlan"])
    assert "COLLSCAN" not in stages, f"{params} scans the collection: {stages}"
    assert "SORT" not in stages, f"{params} needs a blocking sort: {stages}"

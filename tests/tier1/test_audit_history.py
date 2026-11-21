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

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.api.deps import caller
from app.api.pages import MAX_LIMIT
from app.audit.events import AuditEvent, AuditStore, Entry, Filters
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
    app.state.trino = Trino(url="http://127.0.0.1:1")
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
    [event] = response.json()["items"]
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

    [event] = (await client.get(AUDIT)).json()["items"]

    assert event["category"] == "admin"
    assert event["snapshot"] is None


async def test_the_system_is_a_named_actor(client: AsyncClient) -> None:
    """Startup recovery and Auto Rollback change things with nobody on the other end of a
    request. A record reading "apchi reset this" is answerable; one that lost its actor looks
    like a bug in the audit."""
    from app.audit.events import SYSTEM

    await _store(client).record(SYSTEM, "none", "pipeline", "rolled_back", snapshot=3)

    [event] = (await client.get(AUDIT)).json()["items"]

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

    [event] = (await client.get(AUDIT)).json()["items"]

    assert event["outcome"] == "refused"
    assert "frozen" in event["detail"]


# --- ordering and paging ------------------------------------------------------------------


async def test_newest_first(client: AsyncClient) -> None:
    await _some(_store(client), 3)

    events = (await client.get(AUDIT)).json()["items"]

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
        seen += [e["id"] for e in page["items"]]
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

    overlap = {e["id"] for e in first["items"]} & {e["id"] for e in second["items"]}
    assert overlap == set(), "the second page repeated rows from the first"
    assert [e["resource"] for e in second["items"]] == ["catalog_2", "catalog_1", "catalog_0"]


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
        seen += [e["id"] for e in page["items"]]
        cursor = page["next"]
        if cursor is None:
            break

    assert len(set(seen)) == 4, f"paging within one instant lost or repeated rows: {seen}"


async def test_the_last_page_has_no_cursor(client: AsyncClient) -> None:
    """Otherwise a client cannot tell "that was everything" from "ask again"."""
    await _some(_store(client), 2)

    page = (await client.get(AUDIT, params={"limit": 50})).json()

    assert len(page["items"]) == 2
    assert page["next"] is None


async def test_a_mangled_cursor_is_refused_not_a_crash(client: AsyncClient) -> None:
    response = await client.get(AUDIT, params={"before": "not-a-cursor"})

    assert response.status_code == 422, response.text
    assert "before" in response.text


async def test_an_empty_history_is_an_empty_page(client: AsyncClient) -> None:
    response = await client.get(AUDIT)

    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next": None}


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

    assert [e["resource"] for e in response.json()["items"]] == ["wanted"]


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

    assert [e["resource"] for e in recent.json()["items"]] == ["recent"]


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
    assert [e["actor"] for e in response.json()["items"]] == ["sam"]


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
        # An identity: the name its owner gave the token, never what it was used to send.
        "token_name",
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
    # Called directly rather than relying on the first write, which indexes once per process
    # and may already have happened in another test -- so this would pass by doing nothing.
    await store.ensure_indexes(retention_days=None)

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

    await store.ensure_indexes(store._retention_days)

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


#: The two filters that are a range over `at` rather than an equality match on a field of
#: their own, so the base `(at, _id)` index already serves them.
A_TIME_RANGE = {"since", "until"}


async def _plan(store: AuditStore, query: dict[str, Any]) -> str:
    """MongoDB's own plan for one of the endpoint's queries.

    Asserted against the plan rather than by reading the index list, which would prove an
    index exists and not that the query uses it.
    """
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
    return str(plan["queryPlanner"]["winningPlan"])


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"actor": "jo", "category": "catalogs"},
        {"at": {"$gte": datetime(2020, 1, 1, tzinfo=UTC)}},
    ],
    ids=["unfiltered", "two-at-once", "by-time"],
)
async def test_every_query_the_endpoint_makes_uses_an_index(
    client: AsyncClient, query: dict[str, Any]
) -> None:
    """A scan is not a slow endpoint, it is an endpoint that works in testing and times out
    after a year of history -- and a blocking sort is the same failure wearing a different
    name, because it grows with the collection rather than with the page."""
    store = _store(client)
    await _some(store, 5)
    # Directly, for the same reason as the retention test: indexing happens once per process,
    # so this test's own database may not have been the one that got them.
    await store.ensure_indexes(retention_days=None)

    stages = await _plan(store, query)

    assert "COLLSCAN" not in stages, f"{query} scans the collection: {stages}"
    assert "SORT" not in stages, f"{query} needs a blocking sort: {stages}"


async def test_every_filter_the_endpoint_offers_is_indexed(client: AsyncClient) -> None:
    """Derived from the filter model rather than listed, which is the point.

    The parametrized test above used to carry one case per filter, built with a branch per
    parameter -- so adding a filter did not fail it, it just quietly stopped covering the new
    one. Reading `Filters` means an unindexed filter fails here on the day it is added, which
    is the only time the fix is cheap.

    `Filters` is where a filter gets added, because it is what the API declares and what both
    routes take; `Filters.EQUALITY` is what `ensure_indexes` walks. Asserting the two agree
    is the real check -- a filter added to the model and forgotten in the tuple would be
    offered, unindexed, and nothing else would notice.
    """
    store = _store(client)
    await _some(store, 5)
    await store.ensure_indexes(retention_days=None)

    filters = [name for name in Filters.model_fields if name not in A_TIME_RANGE]
    assert sorted(filters) == sorted(Filters.EQUALITY), (
        f"a filter is offered that nothing indexes: {sorted(set(filters) - set(Filters.EQUALITY))}"
    )
    assert sorted(filters) == [
        "action",
        "actor",
        "apply_id",
        "category",
        "outcome",
        "resource",
    ], f"a filter was added or removed without a look at this file: {filters}"

    for name in filters:
        # The value is irrelevant to the plan -- an equality match on a string either has an
        # index for that field or does not.
        stages = await _plan(store, {name: "anything"})
        assert "COLLSCAN" not in stages, f"filtering on {name} scans the collection: {stages}"
        assert "SORT" not in stages, f"filtering on {name} needs a blocking sort: {stages}"


# --- the filters ---------------------------------------------------------------------------


async def _a_mixed_history(store: AuditStore) -> None:
    """One of each shape worth filtering for, written directly.

    Written here rather than driven through the API because what is under test is the filter,
    and producing a `refused` through the API means engaging Maintenance Mode -- which proves
    the freeze works, not that the filter does.
    """
    await store.record("jo", "password", "catalogs", "created", resource="finance_pg")
    await store.record("jo", "password", "catalogs", "updated", resource="finance_pg")
    await store.record(
        "sam", "token", "catalogs", "attempted", resource="finance_pg", outcome="refused"
    )
    await store.record("sam", "token", "event_listeners", "created", resource="audit")
    await store.record("apchi", "none", "pipeline", "applied", snapshot=4, apply_id="apply-4")
    await store.record("apchi", "none", "pipeline", "rolled_back", apply_id="apply-4")
    await store.record("apchi", "none", "pipeline", "applied", snapshot=5, apply_id="apply-5")


async def test_refusals_can_be_asked_for_on_their_own(as_operator: AsyncClient) -> None:
    """The first question anybody opens an audit log to ask, and before this it meant paging a
    category and filtering client-side -- which gets worse every month the trail is kept."""
    await _a_mixed_history(_store(as_operator))

    page = (await as_operator.get(AUDIT, params={"outcome": "refused"})).json()

    assert [event["action"] for event in page["items"]] == ["attempted"]
    assert page["items"][0]["actor"] == "sam"


async def test_one_resource_can_be_followed_across_actors_and_actions(
    as_operator: AsyncClient,
) -> None:
    """ "Everything that happened to finance_pg" -- three events, two actors, three actions,
    one of them refused."""
    await _a_mixed_history(_store(as_operator))

    page = (await as_operator.get(AUDIT, params={"resource": "finance_pg"})).json()

    assert len(page["items"]) == 3
    assert {event["resource"] for event in page["items"]} == {"finance_pg"}
    assert {event["actor"] for event in page["items"]} == {"jo", "sam"}


async def test_an_event_about_no_particular_resource_is_not_returned_by_that_filter(
    as_operator: AsyncClient,
) -> None:
    """`resource` is null on an act against a whole Section or Cluster, and an equality match
    on a name cannot return those. Worth pinning, because a design that filters by resource
    and shows a total is counting a different set from the unfiltered one."""
    await _a_mixed_history(_store(as_operator))

    by_resource = (await as_operator.get(AUDIT, params={"resource": "finance_pg"})).json()
    everything = (await as_operator.get(AUDIT)).json()

    assert all(event["resource"] is not None for event in by_resource["items"])
    assert any(event["resource"] is None for event in everything["items"]), (
        "the fixture has to contain an event with no resource for this to mean anything"
    )


async def test_one_applys_entries_can_be_asked_for_by_its_id(as_operator: AsyncClient) -> None:
    """The id that joins the trail to the logs for the same Apply. Two of the three pipeline
    events belong to one Apply, which is what makes this a filter and not a listing."""
    await _a_mixed_history(_store(as_operator))

    page = (await as_operator.get(AUDIT, params={"apply_id": "apply-4"})).json()

    assert {event["action"] for event in page["items"]} == {"applied", "rolled_back"}
    assert {event["apply_id"] for event in page["items"]} == {"apply-4"}


async def test_filtering_by_action_narrows_within_a_category(as_operator: AsyncClient) -> None:
    await _a_mixed_history(_store(as_operator))

    page = (await as_operator.get(AUDIT, params={"action": "applied"})).json()

    assert [event["snapshot"] for event in page["items"]] == [5, 4], "newest first"


async def test_the_filters_combine_as_a_conjunction(as_operator: AsyncClient) -> None:
    """Not a union. An actor and an outcome together means that actor's refusals, which is the
    question somebody asks during an incident."""
    await _a_mixed_history(_store(as_operator))

    both = (await as_operator.get(AUDIT, params={"actor": "sam", "outcome": "refused"})).json()
    just_sam = (await as_operator.get(AUDIT, params={"actor": "sam"})).json()

    assert len(just_sam["items"]) == 2
    assert len(both["items"]) == 1
    assert both["items"][0]["resource"] == "finance_pg"


async def test_a_filter_value_outside_the_vocabulary_is_refused_not_ignored(
    as_operator: AsyncClient,
) -> None:
    """`outcome` and `action` are closed sets, so a typo is a 422 rather than an empty page.
    An empty page is the more dangerous answer: it reads as "nothing happened".

    `denied` is the plausible wrong guess -- the vocabulary is `refused`, and a client that
    guessed would otherwise be told the trail holds no refusals.
    """
    wrong = await as_operator.get(AUDIT, params={"outcome": "denied"})
    right = await as_operator.get(AUDIT, params={"outcome": "refused"})

    assert wrong.status_code == 422
    assert right.status_code == 200


async def test_filters_survive_the_page_boundary(as_operator: AsyncClient) -> None:
    """A cursor carries no filter, so the caller resends it -- and the second page has to be
    the rest of the *filtered* set rather than the rest of the collection."""
    store = _store(as_operator)
    for index in range(5):
        await store.record(
            "jo", "password", "catalogs", "attempted", resource=f"c_{index}", outcome="refused"
        )
        await store.record("jo", "password", "catalogs", "created", resource=f"c_{index}")

    first = (await as_operator.get(AUDIT, params={"outcome": "refused", "limit": 3})).json()
    assert len(first["items"]) == 3
    assert first["next"] is not None

    second = (
        await as_operator.get(
            AUDIT, params={"outcome": "refused", "limit": 3, "before": first["next"]}
        )
    ).json()

    assert len(second["items"]) == 2
    assert second["next"] is None
    assert all(event["outcome"] == "refused" for event in first["items"] + second["items"])


# --- the export -----------------------------------------------------------------------------

EXPORT = f"{AUDIT}/export"


def _lines(body: str) -> list[dict[str, Any]]:
    """The NDJSON parsed, and the format asserted while doing it: no wrapper object, no
    array, no trailing blank line to skip."""
    assert body.endswith("\n"), "every line is terminated, including the last"
    return [json.loads(line) for line in body.splitlines()]


async def test_the_export_is_the_listing_one_event_per_line(as_operator: AsyncClient) -> None:
    """Each line is exactly the `AuditEvent` the listing returns, which is the reason NDJSON
    was chosen over CSV (#215): no column order to decide, no nulls to flatten, and
    `openapi.json` already documents the shape."""
    await _a_mixed_history(_store(as_operator))
    listed = (await as_operator.get(AUDIT)).json()["items"]

    response = await as_operator.get(EXPORT)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    exported = _lines(response.text)
    # Its own record is the newest event, so it is the first line. Everything after it is the
    # history the listing just returned, in the same order and field for field.
    assert exported[1:] == listed


async def test_the_export_contains_the_record_of_itself(as_operator: AsyncClient) -> None:
    """Recorded before the first byte is written, so a client that hangs up mid-stream still
    leaves the record -- a partial copy of an audit trail being exactly the copy you would
    want to know about. The copy therefore contains the record of itself being taken."""
    await _a_mixed_history(_store(as_operator))

    exported = _lines((await as_operator.get(EXPORT)).text)

    assert exported[0]["action"] == "exported"
    assert exported[0]["category"] == "audit"
    assert exported[0]["actor"] == "jo"
    assert exported[0]["outcome"] == "succeeded"
    assert exported[0]["resource"] is None, "an act on the trail, not on a resource"


async def test_an_export_records_the_filters_it_used(as_operator: AsyncClient) -> None:
    """ "Who took a copy" is only half the question. A copy of one actor's refusals and a copy
    of the whole trail are different acts, and the filters are the only thing that tells them
    apart."""
    await _a_mixed_history(_store(as_operator))

    await as_operator.get(EXPORT, params={"outcome": "refused", "actor": "sam"})
    await as_operator.get(EXPORT)

    exports = (await as_operator.get(AUDIT, params={"action": "exported"})).json()["items"]

    assert [event["detail"] for event in exports] == [
        "an export of the whole trail",
        # Named in the model's order rather than the query string's, so two exports with the
        # same filters leave the same record whatever order the client sent them in.
        "an export filtered by actor=sam, outcome=refused",
    ], "newest first"


async def test_reading_a_page_is_not_audited(as_operator: AsyncClient) -> None:
    """The asymmetry is deliberate. Taking a copy leaves somebody holding the trail outside
    Apchi's rules about it; reading a page in a console does not, and auditing every read
    would bury the exports that matter in a log of people looking at the log."""
    await _a_mixed_history(_store(as_operator))
    before = len((await as_operator.get(AUDIT)).json()["items"])

    await as_operator.get(AUDIT, params={"limit": 3})
    await as_operator.get(AUDIT, params={"outcome": "refused"})

    assert len((await as_operator.get(AUDIT)).json()["items"]) == before


async def test_the_export_is_not_capped_at_a_page(as_operator: AsyncClient) -> None:
    """The point of having it. `limit` cannot exceed 500 (`MAX_LIMIT`), so a trail longer than
    that cannot be taken out of Apchi by paging -- which is the state the collection is in by
    default, since retention is off."""
    store = _store(as_operator)
    count = MAX_LIMIT + 100
    # One write rather than 600, for #69's reason: per-operation traffic is what this suite's
    # MongoDB falls over on.
    await store.record_each(
        "jo",
        "password",
        [
            Entry(category="catalogs", action="created", resource=f"c_{index}")
            for index in range(count)
        ],
    )

    exported = _lines((await as_operator.get(EXPORT)).text)

    assert len(exported) == count + 1, "everything, plus the record of the export itself"
    assert len((await as_operator.get(AUDIT, params={"limit": MAX_LIMIT})).json()["items"]) == (
        MAX_LIMIT
    ), "which is as much as the listing can give in one answer"


async def test_the_export_takes_the_same_filters_as_the_listing(as_operator: AsyncClient) -> None:
    """One set of query parameters, declared once and shared by both routes. An export that
    filtered differently would be a second, undocumented query language over one collection."""
    await _a_mixed_history(_store(as_operator))

    exported = _lines((await as_operator.get(EXPORT, params={"resource": "finance_pg"})).text)

    assert {event["resource"] for event in exported} == {"finance_pg"}
    assert len(exported) == 3, "its own record carries no resource, so the filter excludes it"


async def test_a_filter_value_outside_the_vocabulary_is_refused_by_the_export_too(
    as_operator: AsyncClient,
) -> None:
    """Shared parameters mean shared validation. An export that answered an unknown `outcome`
    with an empty file would read as "nothing happened" in a file somebody keeps."""
    response = await as_operator.get(EXPORT, params={"outcome": "denied"})

    assert response.status_code == 422


async def test_the_export_is_offered_as_a_download(as_operator: AsyncClient) -> None:
    """It is reached from a console by clicking something, and a browser given no disposition
    renders NDJSON as a wall of text in a tab."""
    response = await as_operator.get(EXPORT)

    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment; ")
    assert ".ndjson" in disposition


async def test_an_operator_may_export(as_operator: AsyncClient) -> None:
    """Not Admin-only, for the reason the listing is not: the trail is what makes a shared
    Candidate acceptable (ADR-0002), and the same data in a different shape does not deserve
    a different gate."""
    assert OPERATOR.role == "operator"

    assert (await as_operator.get(EXPORT)).status_code == 200


async def test_the_export_carries_no_configuration_values(as_operator: AsyncClient) -> None:
    """A downloadable copy is the place this would hurt most. The exported line is the
    `AuditEvent` model and nothing else, so a value-bearing field cannot appear in a file
    without first appearing in the model -- which `extra="forbid"` already stops."""
    await _a_mixed_history(_store(as_operator))

    exported = _lines((await as_operator.get(EXPORT)).text)

    assert all(set(event) == set(AuditEvent.model_fields) for event in exported)

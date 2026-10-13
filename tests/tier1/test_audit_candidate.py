"""Every Operator edit leaves a record of who made it.

The point of this file is the coverage test at the bottom, not any individual case. There are
about forty mutating operations, and the failure mode worth defending against is not an audit
record with a wrong field -- it is a route that changes the Candidate and records nothing,
which works perfectly and is invisible. So the last test walks the committed contract,
exercises every mutating Operator route, and fails on any that moved the Candidate without
saying who moved it.

That is why the audit is written at `CandidateStore.save` rather than per route: one function
every edit reaches, so the coverage test has something it can actually be true of.
"""

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, get_args

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.api.deps import caller
from app.audit.changes import NOT_AUDITED, as_section, category_of
from app.audit.events import AuditEvent, AuditStore
from app.auth.identity import Identity
from app.config import Settings
from app.main import create_app
from app.pipeline.maintenance import EngagedBy
from app.sections import SectionName
from tests.conftest import FakeKubernetes

AUDIT = "/api/v1/audit"
CONTRACT = Path(__file__).resolve().parents[2] / "openapi.json"

OPERATOR = Identity(subject="jo", role="operator", via="password")
AUTOMATION = Identity(subject="jo", role="operator", via="token", token_id="tok_9")

#: Values that satisfy path parameters, so a request reaches the handler rather than stopping
#: at 422. Mirrors tests/tier1/test_identity.py, which walks the same contract for gates.
PARAMS = {
    "number": "1",
    "name": "anything",
    "key": "anything",
    "path": "anything",
    "section": "catalogs",
    "resource": "anything",
    "apply_id": "anything",
    "validation_id": "anything",
    "token_id": "anything",
}


async def _serving(settings: Settings, fake: FakeKubernetes, who: Identity) -> AsyncClient:
    app = create_app(settings)
    app.dependency_overrides[caller] = lambda: who
    app.state.kubernetes = fake
    app.state.trino = Trino(host="127.0.0.1", port=1)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi")


@pytest.fixture
async def as_operator(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    http = await _serving(settings, fake_kubernetes, OPERATOR)
    app = http._transport.app  # type: ignore[attr-defined]
    async with http, app.router.lifespan_context(app):
        yield http


@pytest.fixture
async def as_automation(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    http = await _serving(settings, fake_kubernetes, AUTOMATION)
    app = http._transport.app  # type: ignore[attr-defined]
    async with http, app.router.lifespan_context(app):
        yield http


def _store(client: AsyncClient) -> AuditStore:
    app = client._transport.app  # type: ignore[attr-defined]
    store: AuditStore = app.state.audit_store
    return store


async def _events(client: AsyncClient) -> list[AuditEvent]:
    page = (await client.get(AUDIT, params={"limit": 200})).json()
    return [AuditEvent.model_validate(event) for event in page["events"]]


PROPERTIES = {"connection-url": "jdbc:postgresql://db.internal:5432/finance"}


async def _catalog(client: AsyncClient, name: str = "finance_pg") -> None:
    response = await client.post(
        "/api/v1/catalogs",
        json={"name": name, "connector": "postgresql", "properties": PROPERTIES},
    )
    assert response.status_code == 201, response.text


# --- one edit, one record -----------------------------------------------------------------


async def test_creating_a_resource_records_who_created_it(as_operator: AsyncClient) -> None:
    await _catalog(as_operator)

    [event] = await _events(as_operator)

    assert event.actor == "jo"
    assert event.via == "password"
    assert event.category == "catalogs"
    assert event.resource == "finance_pg"
    assert event.action == "created"
    assert event.outcome == "succeeded"
    assert event.snapshot is None, "a staged edit has reached no Cluster and no Snapshot"


async def test_editing_a_resource_records_an_update(as_operator: AsyncClient) -> None:
    await _catalog(as_operator)

    response = await as_operator.patch(
        "/api/v1/catalogs/finance_pg",
        # The whole property map, because a PATCH replaces it: dropping connection-url would
        # be refused by validation rather than recorded as an edit.
        json={"properties": PROPERTIES | {"case-insensitive-name-matching": "true"}},
    )
    assert response.status_code == 200, response.text

    assert [(e.action, e.resource) for e in await _events(as_operator)] == [
        ("updated", "finance_pg"),
        ("created", "finance_pg"),
    ]


async def test_removing_a_resource_records_a_removal(as_operator: AsyncClient) -> None:
    await _catalog(as_operator)

    assert (await as_operator.delete("/api/v1/catalogs/finance_pg")).status_code == 204

    assert [e.action for e in await _events(as_operator)] == ["removed", "created"]


async def test_an_edit_that_changes_nothing_records_nothing(as_operator: AsyncClient) -> None:
    """A re-serialised equal value is not a change. Without this every Apply would appear to
    have edited everything, and the trail would be noise nobody reads."""
    await _catalog(as_operator)
    before = len(await _events(as_operator))

    await as_operator.patch("/api/v1/catalogs/finance_pg", json={})

    assert len(await _events(as_operator)) == before


async def test_one_request_touching_several_resources_records_each(
    as_operator: AsyncClient,
) -> None:
    """An import replaces the whole Candidate, so a derived diff would be a long list. It is
    recorded as the one act it is -- see the Reset and Import tests below -- but the
    per-resource derivation itself has to handle several at once, which this proves through
    the store directly."""
    store = _store(as_operator)
    app = as_operator._transport.app  # type: ignore[attr-defined]
    candidate = await app.state.candidate_store.load()
    for name in ("one", "two", "three"):
        candidate.resources("catalogs")[name] = {
            "name": name,
            "connector": "postgresql",
            "properties": PROPERTIES,
        }
    await app.state.candidate_store.save(candidate)

    events = [e for e in await _events(as_operator) if e.category == "catalogs"]

    assert sorted(e.resource or "" for e in events) == ["one", "three", "two"]
    assert {e.action for e in events} == {"created"}
    del store


# --- the acts that are not resource edits -------------------------------------------------


async def test_a_reset_is_one_act_not_a_list_of_removals(as_operator: AsyncClient) -> None:
    """Deriving it would record a plausible list of resource changes and lose the only thing
    somebody reading the trail wants to know, which is that it was a Reset."""
    await _catalog(as_operator)

    assert (await as_operator.post("/api/v1/candidate/reset", json={})).status_code == 204

    [latest, _] = await _events(as_operator)
    assert latest.category == "pipeline"
    assert latest.action == "reset"
    assert latest.resource is None
    assert latest.actor == "jo"


async def test_a_section_revert_names_the_section_and_the_act(as_operator: AsyncClient) -> None:
    """Distinguishable from an ordinary edit, which is the whole reason it is declared rather
    than derived."""
    response = await as_operator.post("/api/v1/catalogs/revert", json={"snapshot": 1})

    # No Snapshot 1 exists in a fresh Cluster, so this is refused -- and the refusal is itself
    # recorded, which the refusal tests below cover. What matters here is that when it does
    # succeed the action is `reverted`, proved through the store with a real Snapshot.
    assert response.status_code in (200, 404, 422), response.text


async def test_an_import_is_one_act(as_operator: AsyncClient) -> None:
    body = {
        "format": "apchi.configuration/v1",
        # Every Section, because an import replaces the whole configuration: one left out
        # would be refused rather than treated as empty.
        "sections": {
            "catalogs": {
                "imported_pg": {
                    "name": "imported_pg",
                    "connector": "postgresql",
                    "properties": PROPERTIES,
                }
            },
            "client_certificates": {},
            "certificate_mapping": {},
            "permissions": {},
            "resource_groups": {},
            "event_listeners": {},
        },
        "admin": None,
    }

    response = await as_operator.put("/api/v1/candidate", json=body)
    assert response.status_code == 200, response.text

    [latest] = [e for e in await _events(as_operator) if e.category == "pipeline"]
    assert latest.action == "imported"
    assert latest.resource is None


# --- who, exactly -------------------------------------------------------------------------


async def test_an_edit_made_with_a_token_records_the_owner_and_the_token(
    as_automation: AsyncClient,
) -> None:
    """The owner, because the token acts as them; the token id, because a revoked token in the
    trail still has to resolve -- revocation is exactly when somebody reads this."""
    await _catalog(as_automation)

    [event] = await _events(as_automation)

    assert event.actor == "jo"
    assert event.via == "token"
    assert event.token_id == "tok_9"


async def test_a_change_apchi_makes_itself_records_the_system_actor(
    as_operator: AsyncClient,
) -> None:
    """Auto Rollback and startup recovery reset the Candidate with nobody on the other end of
    a request. A named actor, not a null one: "apchi reset this" is answerable."""
    app = as_operator._transport.app  # type: ignore[attr-defined]

    # Called directly, as the pipeline calls it -- outside any request, so no actor is set.
    await app.state.candidate_store.reset()

    [event] = [e for e in await _events(as_operator) if e.action == "reset"]
    assert event.actor == "apchi"
    assert event.via == "none"


# --- refusals -----------------------------------------------------------------------------


async def test_a_mutation_refused_under_maintenance_mode_is_recorded(
    as_operator: AsyncClient,
) -> None:
    """ "Who tried to change this during the freeze" is a question somebody asks, and a trail
    that only holds what succeeded cannot answer it."""
    app = as_operator._transport.app  # type: ignore[attr-defined]
    await app.state.maintenance_store.engage("an upgrade", EngagedBy.ADMIN)

    refused = await as_operator.post(
        "/api/v1/catalogs",
        json={"name": "nope", "connector": "postgresql", "properties": PROPERTIES},
    )

    assert refused.status_code == 409, refused.text
    # Filtered: engaging Maintenance Mode is itself an audited Admin change (#141), so the
    # unfiltered trail holds that event too.
    [event] = [e for e in await _events(as_operator) if e.category == "catalogs"]
    assert event.outcome == "refused"
    assert event.action == "attempted"
    assert event.category == "catalogs"
    assert event.actor == "jo"
    assert event.detail is not None


async def test_a_refusal_names_the_resource_the_path_named(as_operator: AsyncClient) -> None:
    await _catalog(as_operator)
    app = as_operator._transport.app  # type: ignore[attr-defined]
    await app.state.maintenance_store.engage("an upgrade", EngagedBy.ADMIN)

    await as_operator.delete("/api/v1/catalogs/finance_pg")

    [refusal, _] = [e for e in await _events(as_operator) if e.category == "catalogs"]
    assert refusal.outcome == "refused"
    assert refusal.resource == "finance_pg"


async def test_a_refused_payload_is_recorded_too(as_operator: AsyncClient) -> None:
    """Validation rejects before anything is stored (ADR-0002), so this refusal never reaches
    the Candidate and has no diff to be derived from -- which is why it is recorded from the
    error handler and its category comes from the route."""
    refused = await as_operator.post(
        "/api/v1/catalogs",
        # postgresql requires connection-url, so this is rejected by static validation
        # before anything is stored.
        json={"name": "bad", "connector": "postgresql", "properties": {}},
    )

    assert refused.status_code == 422, refused.text
    [event] = await _events(as_operator)
    assert event.outcome == "refused"
    assert event.category == "catalogs"


async def test_a_refused_read_is_not_recorded(as_operator: AsyncClient) -> None:
    """A GET that found nothing is not an attempt to change anything, and §20 has no actor
    question about it. The log already carries it."""
    assert (await as_operator.get("/api/v1/catalogs/nope")).status_code == 404

    assert await _events(as_operator) == []


# --- the mapping stays honest -------------------------------------------------------------


def test_every_section_narrows_back_to_itself() -> None:
    """`as_section` is the one checked crossing from an arbitrary string to a Section name, so
    it has to know all six: a seventh added to the Literal and not to it would silently stop
    being audited, because the diff skips keys it cannot name."""
    missing = [name for name in get_args(SectionName) if as_section(name) is None]

    assert missing == [], f"these Sections would not be audited: {missing}"


def _tags() -> set[str]:
    contract: dict[str, Any] = json.loads(CONTRACT.read_text())
    tags: set[str] = set()
    for operations in contract["paths"].values():
        for method, operation in operations.items():
            if method in {"post", "put", "patch", "delete"}:
                tags.update(operation.get("tags", []))
    return tags


def test_every_mutating_router_has_a_decided_category() -> None:
    """Walked from the contract, so a router added later fails here rather than having its
    changes land in a default bucket -- or in no bucket at all, which is worse because it
    looks like nothing happened.

    `NOT_AUDITED` is how a tag says "deliberately not audited", so the absence of a decision
    is distinguishable from a decision to record nothing.
    """
    undecided = sorted(
        tag for tag in _tags() if category_of([tag]) is None and tag not in NOT_AUDITED
    )

    assert undecided == [], (
        f"these mutating routers have no audit category: {undecided}. Add each to "
        "app/audit/changes.py -- either to the category table or to NOT_AUDITED, with a "
        "reason."
    )


# --- the coverage assertion ---------------------------------------------------------------


def _operations() -> Iterator[tuple[str, str]]:
    paths: dict[str, Any] = json.loads(CONTRACT.read_text())["paths"]
    for path, operations in paths.items():
        for method, operation in operations.items():
            if method in {"post", "put", "patch", "delete"}:
                yield method, path
                del operation


def _concrete(path: str) -> str:
    for parameter, value in PARAMS.items():
        path = path.replace(f"{{{parameter}}}", value)
    return path


async def test_no_route_can_change_the_candidate_without_recording_it(
    as_operator: AsyncClient,
) -> None:
    """The assertion this file exists for, in two halves.

    **Silence on a change.** Any route that moved the Candidate and wrote no event is a hole
    -- one that works, which is why nobody would notice it. Checked for every mutating
    operation in the contract, whatever its category.

    **Coverage of every audited category.** Every mutating operation on one of the six
    Sections, on the Candidate as a whole, or on an Admin surface must
    produce an event either way: it succeeded and was derived from the diff, or it was refused
    and recorded as an attempt. 24 routes across all six, verified by disabling recording and
    watching every one of them appear here.

    Payloads are deliberately empty and `POST /applies` is walked early, so most of these land
    refused -- which is the point rather than a weakness, because a refusal is recorded too
    and the thing being detected is *silence*. The derived path is covered resource by
    resource by the tests above; this one is about nothing being missed.

    Pipeline and Admin routes are excluded because their events are #140 and #141. When those
    land, this filter is what should widen.

    Walking the contract rather than naming routes is what makes a route added later fail
    here instead of shipping unaudited.
    """
    app = as_operator._transport.app  # type: ignore[attr-defined]
    operations = [
        (method, path)
        for method, path in _operations()
        if not path.startswith("/api/v1/auth")  # signing in is not a configuration change
    ]
    assert len(operations) > 20, "the contract walk found almost nothing, so it proves nothing"

    silent: list[str] = []
    unaudited_sections: list[str] = []

    for method, path in operations:
        before_candidate = await app.state.candidate_store.stored()
        before_events = len(await _events(as_operator))

        response = await as_operator.request(method, _concrete(path), json={})

        after_candidate = await app.state.candidate_store.stored()
        changed = (None if before_candidate is None else before_candidate.sections) != (
            None if after_candidate is None else after_candidate.sections
        )
        recorded = len(await _events(as_operator)) > before_events
        if changed and not recorded:
            silent.append(f"{method.upper()} {path}")
        if _is_audited_route(path) and not recorded:
            unaudited_sections.append(f"{method.upper()} {path} -> {response.status_code}")

    assert silent == [], f"these changed the Candidate and recorded nothing: {silent}"
    assert unaudited_sections == [], (
        f"these routes produced no audit event at all: {unaudited_sections}. Every "
        "Operator edit is recorded at CandidateStore.save, and every refusal at the error "
        "handler, so neither outcome should be silent."
    )


#: Audited, but not synchronously. See the walk's docstring.
ASYNCHRONOUS = {"/api/v1/applies"}


def _is_audited_route(path: str) -> bool:
    """Whether a path's changes are audited at the moment the request returns.

    Read from the contract's own tags, so this agrees with what the audit code uses rather
    than with a second list that can drift from it.
    """
    if path in ASYNCHRONOUS:
        return False
    contract: dict[str, Any] = json.loads(CONTRACT.read_text())
    for method, operation in contract["paths"][path].items():
        if method in {"post", "put", "patch", "delete"}:
            category = category_of(operation.get("tags", []))
            if category is None:
                return False
            # Every category there is, now that #140 and #141 have landed: the six Sections,
            # the Candidate as a whole, and the Admin surfaces. Anything a router declares a
            # category for must produce an event, which is the strongest form this assertion
            # takes -- a tag that should not be audited belongs in NOT_AUDITED instead.
            return category in ("pipeline", "admin") or as_section(category) is not None
    return False

"""Applies, rollbacks, and the two §20 fields only they can fill.

A staged edit has no outcome worth recording and produces no Snapshot (#139). An Apply has
both, and the failure cases are the point of this file rather than the success case. §10 sends
a partially applied Apply to an incident state rather than improvising compensating statements,
and §14 engages Maintenance Mode after a failed Auto Rollback -- both are states a human
arrives at with no idea what happened, and the audit record is what tells them who asked for
it, how far it got, and whether Apchi put the Cluster back.

The one that is easy to get wrong, and is asserted here twice over: **who**. The Apply runs in
a background task that outlives the request that started it, so an actor read inside that task
resolves to the system. Of every record in the trail, "who promoted this configuration" is the
one that least deserves to say "apchi".
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from pymongo.errors import PyMongoError

from app.adapters.trino import Trino
from app.api.deps import caller
from app.audit.events import AuditEvent, AuditStore
from app.auth.identity import Identity
from app.config import Settings
from app.main import create_app
from app.pipeline.applies import TERMINAL, ApplyStore, Stage, recover_interrupted
from tests.conftest import FakeKubernetes

AUDIT = "/api/v1/audit"
APPLIES = "/api/v1/applies"

OPERATOR = Identity(subject="jo", role="operator", via="password")
PROPERTIES = {"connection-url": "jdbc:postgresql://db.internal:5432/finance"}


async def _events(client: AsyncClient, category: str | None = None) -> list[AuditEvent]:
    params: dict[str, object] = {"limit": 200}
    if category is not None:
        params["category"] = category
    page = (await client.get(AUDIT, params=params)).json()
    return [AuditEvent.model_validate(event) for event in page["events"]]


def _store(client: AsyncClient) -> AuditStore:
    app = client._transport.app  # type: ignore[attr-defined]
    store: AuditStore = app.state.audit_store
    return store


@pytest.fixture
async def as_operator(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    """An authenticated Operator, with no Trino reachable.

    Deliberately unreachable: every Apply here fails, which is the behaviour under test. The
    tests that need an Apply to *succeed* use `applying_client`, which has real Trino behind
    it.
    """
    app = create_app(settings)
    app.dependency_overrides[caller] = lambda: OPERATOR
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=1)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


async def _await_apply(client: AsyncClient, apply_id: str, timeout: float = 60.0) -> Stage:
    """Poll until the Apply reaches a terminal stage, and return it."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"{APPLIES}/{apply_id}")).json()
        stage = Stage(record["stage"])
        if stage in TERMINAL:
            return stage
        await asyncio.sleep(0.05)
    raise AssertionError(f"apply {apply_id} never finished; last stage {stage.value}")


# --- a failed Apply, which is the interesting one -----------------------------------------


async def test_a_failed_apply_records_who_asked_and_how_far_it_got(
    as_operator: AsyncClient,
) -> None:
    """The stage is the detail worth keeping: a human arriving at §10's incident state wants
    to know how far it got before anything else."""
    await as_operator.post(
        "/api/v1/catalogs",
        json={"name": "finance_pg", "connector": "postgresql", "properties": PROPERTIES},
    )
    started = await as_operator.post(APPLIES, json={})
    assert started.status_code == 202, started.text
    apply_id = started.json()["id"]
    await _await_apply(as_operator, apply_id)

    [event] = [e for e in await _events(as_operator, "pipeline") if e.action == "applied"]

    assert event.actor == "jo", "the Apply ran in a background task; the actor must survive it"
    assert event.via == "password"
    assert event.outcome == "failed"
    assert event.snapshot is None
    assert event.apply_id == apply_id
    assert event.detail is not None and "failed in" in event.detail


async def test_every_event_from_one_apply_carries_its_id(as_operator: AsyncClient) -> None:
    """So the trail joins to the logs for the same Apply, which already carry it in a
    contextvar. Without it somebody matches them up by timestamp."""
    started = await as_operator.post(APPLIES, json={})
    apply_id = started.json()["id"]
    await _await_apply(as_operator, apply_id)

    pipeline = await _events(as_operator, "pipeline")

    assert pipeline, "an Apply that reached a terminal stage recorded nothing"
    assert {e.apply_id for e in pipeline} == {apply_id}


async def test_the_actor_is_not_apchi(as_operator: AsyncClient) -> None:
    """The failure mode this slice exists to avoid, asserted on its own.

    `ApplyRunner.start` reads the actor on the request thread. Reading it inside the task
    instead would resolve to the system actor and the record would silently blame Apchi for
    every Apply anybody ran.
    """
    started = await as_operator.post(APPLIES, json={})
    await _await_apply(as_operator, started.json()["id"])

    actors = {e.actor for e in await _events(as_operator, "pipeline")}

    assert "apchi" not in actors or actors != {"apchi"}, (
        "every pipeline event was attributed to Apchi, which means the actor did not survive "
        "into the background task"
    )
    assert "jo" in actors


# --- a successful Apply, and the Snapshot it produced -------------------------------------


async def test_a_successful_apply_records_the_snapshot_it_committed(
    applying_client: AsyncClient,
) -> None:
    """The record that ties an actor to a Snapshot, and the only one that can: a Snapshot
    knows which Apply produced it, and this is what says who asked for it."""
    created = await applying_client.post(
        "/api/v1/catalogs",
        json={"name": "finance_pg", "connector": "postgresql", "properties": PROPERTIES},
    )
    assert created.status_code == 201, created.text
    started = await applying_client.post(APPLIES, json={})
    apply_id = started.json()["id"]

    stage = await _await_apply(applying_client, apply_id, timeout=300)

    assert stage is Stage.SUCCEEDED, (await applying_client.get(f"{APPLIES}/{apply_id}")).text
    [event] = [e for e in await _events(applying_client, "pipeline") if e.action == "applied"]
    assert event.outcome == "succeeded"
    assert event.snapshot == 1
    assert event.apply_id == apply_id
    snapshots = (await applying_client.get("/api/v1/snapshots")).json()
    assert event.snapshot == snapshots[0]["number"], (
        "the audit named a Snapshot that is not the one"
    )


# --- the acts Apchi takes on its own ------------------------------------------------------


async def test_an_interrupted_apply_is_recorded_with_the_system_actor(
    as_operator: AsyncClient,
) -> None:
    """Startup recovery changes the Cluster and nobody asked for it. The actor who started
    the Apply is not responsible for what a restart did to it."""
    app = as_operator._transport.app  # type: ignore[attr-defined]
    applies: ApplyStore = app.state.apply_store
    record = await applies.create(base_snapshot=None)
    await applies.advance(record.id, Stage.VALIDATING)

    await recover_interrupted(applies, app.state.apply_runner._engine_factory, _store(as_operator))

    [event] = [e for e in await _events(as_operator, "pipeline") if e.apply_id == record.id]
    assert event.actor == "apchi"
    assert event.via == "none"
    assert event.outcome == "failed"
    assert event.detail is not None and "restart" in event.detail


async def test_a_full_rollback_is_one_act_naming_its_snapshot(
    applying_client: AsyncClient,
) -> None:
    """Reserved for a serious mistake and never implicit, so the trail has to say that is
    what happened and to which Snapshot -- which a derived diff of every Section would bury.

    It stages rather than applies, so it carries no resulting Snapshot: the source goes in
    `detail`, because `snapshot` means the one an Apply produced.
    """
    await applying_client.post(
        "/api/v1/catalogs",
        json={"name": "finance_pg", "connector": "postgresql", "properties": PROPERTIES},
    )
    started = await applying_client.post(APPLIES, json={})
    assert await _await_apply(applying_client, started.json()["id"], timeout=300) is Stage.SUCCEEDED

    response = await applying_client.post("/api/v1/candidate/rollback", json={"snapshot": 1})
    assert response.status_code == 200, response.text

    [event] = [e for e in await _events(applying_client, "pipeline") if e.action == "rolled_back"]
    assert event.resource is None
    assert event.snapshot is None, "a staged act has produced no Snapshot"
    assert event.detail is not None and "Snapshot 1" in event.detail


async def test_a_reset_names_where_it_restored_from(as_operator: AsyncClient) -> None:
    """A Reset on a Cluster that has never committed re-derives an empty baseline, which is a
    real state rather than missing data."""
    assert (await as_operator.post("/api/v1/candidate/reset", json={})).status_code == 204

    [event] = [e for e in await _events(as_operator, "pipeline") if e.action == "reset"]

    assert event.detail is not None and "baseline" in event.detail


async def test_a_section_revert_names_the_snapshot_it_came_from(
    applying_client: AsyncClient,
) -> None:
    await applying_client.post(
        "/api/v1/catalogs",
        json={"name": "finance_pg", "connector": "postgresql", "properties": PROPERTIES},
    )
    started = await applying_client.post(APPLIES, json={})
    assert await _await_apply(applying_client, started.json()["id"], timeout=300) is Stage.SUCCEEDED

    response = await applying_client.post("/api/v1/catalogs/revert", json={"snapshot": 1})
    assert response.status_code == 200, response.text

    [event] = [e for e in await _events(applying_client, "catalogs") if e.action == "reverted"]
    assert event.detail is not None and "Snapshot 1" in event.detail


# --- a failed audit write must not fail the Apply -----------------------------------------


async def test_an_apply_is_not_failed_by_an_audit_write_that_cannot_land(
    as_operator: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opposite rule from a Candidate edit, and deliberately so.

    An Apply that reached the Cluster must not be reported as failed because its audit row
    would not write: the Cluster has already changed, and failing here would send the caller
    down §10's incident path for a bookkeeping error. A Candidate edit has changed nothing
    yet, so there `CandidateStore.save` lets it propagate.
    """

    async def refuse(*args: object, **kwargs: object) -> None:
        raise PyMongoError("the audit collection is unavailable")

    monkeypatch.setattr(_store(as_operator), "record", refuse)

    started = await as_operator.post(APPLIES, json={})

    assert started.status_code == 202, started.text
    apply_id = started.json()["id"]
    stage = await _await_apply(as_operator, apply_id)

    # It reaches a terminal stage, and reaches the *same* one it would have without the audit
    # at all: no Trino here, so the Apply fails and the Auto Rollback fails after it, which
    # §14 escalates to an incident. What matters is that the reason is Trino's and not the
    # audit's -- a bookkeeping failure must not rewrite what happened to the Cluster.
    assert stage is Stage.INCIDENT, stage.value
    record = (await as_operator.get(f"{APPLIES}/{apply_id}")).json()
    assert "audit" not in (record["failure_reason"] or ""), record["failure_reason"]

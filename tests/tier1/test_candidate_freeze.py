"""The Candidate is frozen for the whole of an Apply, and released after a crash.

Freezing is what makes "what is committed is what was verified" true. Releasing is
the part that must never be left to chance: an Apchi crash mid-Apply would
otherwise freeze the Candidate permanently.
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app
from app.pipeline.applies import ApplyStore, Stage

PG = {
    "name": "finance",
    "connector": "postgresql",
    "properties": {"connection-url": "jdbc:postgresql://db:5432/f"},
}


class SlowEngine:
    """Holds the Apply open so the freeze is observable."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def validate(self) -> None:
        await self.release.wait()

    async def apply(self) -> None: ...
    async def verify(self) -> None: ...
    async def commit(self) -> int | None:
        return None


@pytest.fixture
async def frozen(settings: Settings):
    """A client with an Apply deliberately stuck in its first stage."""
    engine = SlowEngine()
    app = create_app(settings)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as client,
        app.router.lifespan_context(app),
    ):
        app.state.apply_runner._engine = engine
        started = await client.post("/api/v1/applies")
        yield client, started.json()["id"], engine
        engine.release.set()


async def test_mutations_are_rejected_while_an_apply_is_in_flight(frozen) -> None:
    client, apply_id, _ = frozen

    blocked = await client.post("/api/v1/catalogs", json=PG)

    assert blocked.status_code == 409
    assert blocked.json()["code"] == "conflict"
    assert apply_id in blocked.json()["message"]


async def test_the_rejection_names_the_apply_holding_the_freeze(frozen) -> None:
    """So an Operator knows to wait rather than assume something is broken."""
    client, apply_id, _ = frozen

    message = (await client.post("/api/v1/catalogs", json=PG)).json()["message"]

    assert "frozen" in message and "validating" in message


async def test_edits_deletes_and_reset_are_all_frozen(frozen) -> None:
    client, _, _ = frozen

    assert (await client.patch("/api/v1/catalogs/x", json={})).status_code == 409
    assert (await client.delete("/api/v1/catalogs/x")).status_code == 409
    assert (await client.post("/api/v1/candidate/reset")).status_code == 409


async def test_reads_are_unaffected_by_the_freeze(frozen) -> None:
    client, _, _ = frozen

    assert (await client.get("/api/v1/catalogs")).status_code == 200
    assert (await client.get("/api/v1/review")).status_code == 200


async def test_a_second_apply_is_refused_while_one_is_in_flight(frozen) -> None:
    client, _, _ = frozen

    assert (await client.post("/api/v1/applies")).status_code == 409


async def test_the_freeze_lifts_once_the_apply_finishes(frozen) -> None:
    client, apply_id, engine = frozen

    engine.release.set()
    for _ in range(200):
        if (await client.get(f"/api/v1/applies/{apply_id}")).json()["stage"] == "succeeded":
            break
        await asyncio.sleep(0.02)

    assert (await client.post("/api/v1/catalogs", json=PG)).status_code == 201


async def test_startup_recovery_releases_a_candidate_frozen_by_a_crash(
    settings: Settings,
) -> None:
    """An Apply left in flight by a restart is resolved, and the Candidate is
    unfrozen -- whatever else recovery decides, that part cannot be skipped."""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        store: ApplyStore = app.state.apply_store
        stranded = await store.create(base_snapshot=None)

    assert (await _in_flight(settings)) == stranded.id  # still frozen before restart

    # A fresh app is what a restart looks like.
    restarted = create_app(settings)
    async with (
        AsyncClient(transport=ASGITransport(app=restarted), base_url="http://apchi") as client,
        restarted.router.lifespan_context(restarted),
    ):
        recovered = (await client.get(f"/api/v1/applies/{stranded.id}")).json()

        assert recovered["stage"] == Stage.FAILED.value
        assert "restarted" in recovered["failure_reason"]
        assert (await client.post("/api/v1/catalogs", json=PG)).status_code == 201


async def _in_flight(settings: Settings) -> str | None:
    app = create_app(settings)
    app.state.mongo = None  # not needed; we only want the store
    from app.adapters.mongo import Mongo

    mongo = Mongo(settings)
    try:
        record = await ApplyStore(mongo.database).in_flight()
        return None if record is None else record.id
    finally:
        await mongo.close()

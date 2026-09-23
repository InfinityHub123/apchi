"""Apply as a resource.

An Apply runs Validation, Apply, Verification and Commit -- minutes, not a request.
So it is a resource with an identifier, and its record lives in MongoDB: the event
stream is a view over durable state rather than over in-memory progress.

The record holds the *full* stage history, not just the current stage, so a client
reconnecting mid-Apply replays what it missed instead of seeing a blank timeline.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field
from pymongo.asynchronous.database import AsyncDatabase

from app.logging import apply_id_var
from app.pipeline.auto_rollback import INCIDENT_MESSAGE

logger = logging.getLogger(__name__)


class Stage(StrEnum):
    VALIDATING = "validating"
    APPLYING = "applying"
    VERIFYING = "verifying"
    COMMITTING = "committing"
    #: Auto Rollback, on its way to one of the two failed terminals.
    ROLLING_BACK = "rolling_back"
    SUCCEEDED = "succeeded"
    #: The Apply did not go through and the Cluster is back on its latest Snapshot.
    FAILED = "failed"
    #: The Apply did not go through and Auto Rollback could not put the Cluster back.
    #: Apchi has stopped touching it and Maintenance Mode is engaged.
    INCIDENT = "incident"


#: An Apply in one of these is over. Anything else means it is in flight, and the
#: Candidate is frozen. An incident is over in this sense too: nothing further will
#: happen to it, and what keeps Operators off the Cluster is Maintenance Mode, not a
#: freeze that would also block the Admin action that clears it.
TERMINAL: frozenset[Stage] = frozenset({Stage.SUCCEEDED, Stage.FAILED, Stage.INCIDENT})

#: The order the pipeline walks. Stages stay separate internally even where the UI
#: presents them as one action.
PIPELINE: tuple[Stage, ...] = (Stage.VALIDATING, Stage.APPLYING, Stage.VERIFYING, Stage.COMMITTING)

#: Failing in one of these means the Cluster was touched, so Auto Rollback runs.
#:
#: Validation is excluded because nothing reached the Cluster -- there is nothing to
#: undo, and the Candidate is left for the Operator to fix. Commit is excluded for the
#: opposite reason: the configuration was applied *and verified*, and what failed was
#: MongoDB. Rolling back there would tear down a healthy Cluster to recover from a
#: database error.
ROLLED_BACK_FROM: frozenset[Stage] = frozenset({Stage.APPLYING, Stage.VERIFYING})


class Rollback(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class StageEvent(BaseModel):
    stage: Stage
    at: datetime
    detail: str | None = None


class ApplyRecord(BaseModel):
    id: str
    stage: Stage
    history: list[StageEvent] = Field(default_factory=list)
    failure_reason: str | None = None
    interrupted: bool = Field(
        default=False,
        description=(
            "True when an Apchi restart ended this Apply rather than a failure in it. "
            "The Cluster was still put back; what differs is why."
        ),
    )
    rollback: Rollback | None = Field(
        default=None,
        description=(
            "The outcome of Auto Rollback. Absent when none was attempted, which "
            "means nothing reached the Cluster."
        ),
    )
    operator_message: str | None = Field(
        default=None,
        description="What to show an Operator. Set when an Apply ends in an incident.",
    )
    base_snapshot: int | None = None
    snapshot: int | None = Field(
        default=None, description="The Snapshot this Apply committed, when it succeeded."
    )
    started_at: datetime
    finished_at: datetime | None = None

    @property
    def in_flight(self) -> bool:
        return self.stage not in TERMINAL


class ApplyStore:
    def __init__(self, database: AsyncDatabase[dict[str, Any]]) -> None:
        self._collection = database["applies"]

    @staticmethod
    def _as_record(document: dict[str, Any]) -> ApplyRecord:
        document.pop("_id", None)
        return ApplyRecord.model_validate(document)

    async def create(self, base_snapshot: int | None) -> ApplyRecord:
        now = datetime.now(UTC)
        record = ApplyRecord(
            id=f"apl_{uuid.uuid4().hex[:16]}",
            stage=Stage.VALIDATING,
            history=[StageEvent(stage=Stage.VALIDATING, at=now)],
            base_snapshot=base_snapshot,
            started_at=now,
        )
        await self._collection.insert_one({"_id": record.id, **record.model_dump(mode="json")})
        return record

    async def get(self, apply_id: str) -> ApplyRecord | None:
        document = await self._collection.find_one({"_id": apply_id})
        return None if document is None else self._as_record(document)

    async def list(self, limit: int = 50) -> list[ApplyRecord]:
        cursor = self._collection.find().sort("started_at", -1).limit(limit)
        return [self._as_record(document) async for document in cursor]

    async def in_flight(self) -> ApplyRecord | None:
        """The Apply currently freezing the Candidate, if any."""
        document = await self._collection.find_one({"stage": {"$nin": list(TERMINAL)}})
        return None if document is None else self._as_record(document)

    async def advance(self, apply_id: str, stage: Stage, detail: str | None = None) -> None:
        event = StageEvent(stage=stage, at=datetime.now(UTC), detail=detail)
        update: dict[str, Any] = {
            "$set": {"stage": stage.value},
            "$push": {"history": event.model_dump(mode="json")},
        }
        if stage in TERMINAL:
            update["$set"]["finished_at"] = event.at.isoformat()
        await self._collection.update_one({"_id": apply_id}, update)

    async def record_snapshot(self, apply_id: str, number: int) -> None:
        await self._collection.update_one({"_id": apply_id}, {"$set": {"snapshot": number}})

    async def fail(self, apply_id: str, reason: str) -> None:
        await self._collection.update_one({"_id": apply_id}, {"$set": {"failure_reason": reason}})
        await self.advance(apply_id, Stage.FAILED, detail=reason)

    async def mark_interrupted(self, apply_id: str) -> None:
        await self._collection.update_one({"_id": apply_id}, {"$set": {"interrupted": True}})

    async def record_rollback(self, apply_id: str, outcome: Rollback) -> None:
        await self._collection.update_one({"_id": apply_id}, {"$set": {"rollback": outcome.value}})

    async def declare_incident(self, apply_id: str, reason: str, message: str) -> None:
        """The other failed terminal. The failure reason stays the diagnostic one --
        an Operator has to be able to diagnose without log access -- and the message
        is what the UI shows."""
        await self._collection.update_one(
            {"_id": apply_id},
            {"$set": {"failure_reason": reason, "operator_message": message}},
        )
        await self.advance(apply_id, Stage.INCIDENT, detail=message)


class ApplyEngine(Protocol):
    """What the pipeline does at each stage."""

    async def validate(self) -> None: ...
    async def apply(self) -> None: ...
    async def verify(self) -> None: ...
    async def commit(self) -> int | None: ...

    async def roll_back(self) -> None:
        """Put the Cluster back on its latest Snapshot. Raises if it cannot."""
        ...

    async def declare_incident(self, reason: str) -> None:
        """Stop touching the Cluster: engage Maintenance Mode and alert."""
        ...


class NoOpEngine:
    """Does nothing. Used where the stages matter but the work does not."""

    async def validate(self) -> None: ...
    async def apply(self) -> None: ...
    async def verify(self) -> None: ...
    async def commit(self) -> int | None:
        return None

    async def roll_back(self) -> None: ...
    async def declare_incident(self, reason: str) -> None: ...


#: Builds the engine for one Apply. A factory rather than a single instance because
#: an engine carries the state its stages share.
EngineFactory = Callable[[str], ApplyEngine]


class ApplyRunner:
    """Drives an Apply through its stages, in the background."""

    def __init__(self, store: ApplyStore, engine_factory: EngineFactory) -> None:
        self._store = store
        self._engine_factory = engine_factory
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, record: ApplyRecord) -> None:
        task = asyncio.create_task(self._run(record.id))
        # Hold a reference: a bare create_task may be garbage collected mid-flight.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, apply_id: str) -> None:
        # Every record emitted from here carries the identifier, so one id yields
        # the whole story of a failure.
        token = apply_id_var.set(apply_id)
        engine = self._engine_factory(apply_id)
        steps: tuple[tuple[Stage, Callable[[], Awaitable[Any]]], ...] = (
            (Stage.VALIDATING, engine.validate),
            (Stage.APPLYING, engine.apply),
            (Stage.VERIFYING, engine.verify),
            (Stage.COMMITTING, engine.commit),
        )
        reached = Stage.VALIDATING
        try:
            snapshot: int | None = None
            for index, (stage, step) in enumerate(steps):
                reached = stage
                if index:  # the first stage is recorded at creation
                    await self._store.advance(apply_id, stage)
                logger.info("apply stage", extra={"stage": stage.value})
                result = await step()
                if stage is Stage.COMMITTING and isinstance(result, int):
                    snapshot = result
            if snapshot is not None:
                await self._store.record_snapshot(apply_id, snapshot)
            await self._store.advance(apply_id, Stage.SUCCEEDED)
            logger.info("apply succeeded", extra={"snapshot": snapshot})
        except Exception as exc:
            logger.exception("apply failed")
            reason = f"{type(exc).__name__}: {exc}"
            if reached in ROLLED_BACK_FROM:
                await self._roll_back(apply_id, engine, reason)
            else:
                await self._store.fail(apply_id, reason)
        finally:
            apply_id_var.reset(token)

    async def _roll_back(self, apply_id: str, engine: ApplyEngine, reason: str) -> None:
        """One attempt, and no retry whatever it does.

        A failure to roll back is not a failure of this Apply to report and move on
        from: the Cluster is in a state nobody has described, so the outcome is an
        incident and Apchi stops touching it.
        """
        await self._store.advance(apply_id, Stage.ROLLING_BACK, detail=reason)
        logger.warning("rolling back to the latest snapshot", extra={"why": reason})
        try:
            await engine.roll_back()
        except Exception as rollback_exc:
            logger.exception("auto rollback failed")
            await self._store.record_rollback(apply_id, Rollback.FAILED)
            combined = f"{reason}. Auto Rollback then failed: {rollback_exc}"
            await engine.declare_incident(combined)
            await self._store.declare_incident(apply_id, combined, INCIDENT_MESSAGE)
            return
        await self._store.record_rollback(apply_id, Rollback.SUCCEEDED)
        await self._store.fail(apply_id, reason)
        logger.info("cluster returned to its latest snapshot")

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()


INTERRUPTED_REASON = "Apchi restarted while this Apply was in flight; it was not completed."


async def recover_interrupted(store: ApplyStore, engine_factory: EngineFactory) -> list[str]:
    """Resolve Applies left in flight by a restart.

    The Candidate is frozen for the whole of an Apply, so an Apchi crash mid-flight
    would freeze it permanently. Whatever else recovery decides, the Candidate must
    end up unfrozen -- that is the part that must never be left to chance, which is why
    the record is resolved before the Cluster is touched.

    An interrupted Apply may also have left the Cluster diverged: Apply writes the
    catalog Secret before issuing DDL, so a crash in that window leaves the durable copy
    naming a catalog Trino never got. Recovery therefore rolls the Cluster back exactly
    as a failed Apply does -- one attempt, no retry, no Snapshot -- and escalates to an
    incident if that attempt fails. It can do this because the rollback plan is a diff of
    two durable records and needs nothing from the process that died.
    """
    recovered: list[str] = []
    while (record := await store.in_flight()) is not None:
        reached = record.stage
        await store.mark_interrupted(record.id)
        await store.fail(record.id, INTERRUPTED_REASON)
        logger.warning(
            "recovered interrupted apply",
            extra={"recovered_apply": record.id, "reached": reached.value},
        )
        recovered.append(record.id)

        if reached in ROLLED_BACK_FROM:
            await _restore_after_interruption(store, engine_factory, record.id)
    return recovered


async def _restore_after_interruption(
    store: ApplyStore, engine_factory: EngineFactory, apply_id: str
) -> None:
    """The same bounded attempt Auto Rollback makes, from a process that did not run
    the Apply."""
    token = apply_id_var.set(apply_id)
    try:
        engine = engine_factory(apply_id)
        try:
            await engine.roll_back()
        except Exception as exc:
            logger.exception("rollback of an interrupted apply failed")
            await store.record_rollback(apply_id, Rollback.FAILED)
            reason = f"{INTERRUPTED_REASON} Auto Rollback then failed: {exc}"
            await engine.declare_incident(reason)
            await store.declare_incident(apply_id, reason, INCIDENT_MESSAGE)
            return
        await store.record_rollback(apply_id, Rollback.SUCCEEDED)
        logger.info("cluster returned to its latest snapshot after a restart")
    finally:
        apply_id_var.reset(token)

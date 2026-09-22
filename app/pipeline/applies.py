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

logger = logging.getLogger(__name__)


class Stage(StrEnum):
    VALIDATING = "validating"
    APPLYING = "applying"
    VERIFYING = "verifying"
    COMMITTING = "committing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: An Apply in one of these is over. Anything else means it is in flight, and the
#: Candidate is frozen.
TERMINAL: frozenset[Stage] = frozenset({Stage.SUCCEEDED, Stage.FAILED})

#: The order the pipeline walks. Stages stay separate internally even where the UI
#: presents them as one action.
PIPELINE: tuple[Stage, ...] = (Stage.VALIDATING, Stage.APPLYING, Stage.VERIFYING, Stage.COMMITTING)


class StageEvent(BaseModel):
    stage: Stage
    at: datetime
    detail: str | None = None


class ApplyRecord(BaseModel):
    id: str
    stage: Stage
    history: list[StageEvent] = Field(default_factory=list)
    failure_reason: str | None = None
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

    async def fail(self, apply_id: str, reason: str) -> None:
        await self._collection.update_one({"_id": apply_id}, {"$set": {"failure_reason": reason}})
        await self.advance(apply_id, Stage.FAILED, detail=reason)


class ApplyEngine(Protocol):
    """What the pipeline does at each stage.

    Slice 1 fills these in ticket by ticket; this ticket builds the machinery that
    drives them and proves it survives a crash.
    """

    async def validate(self) -> None: ...
    async def apply(self) -> None: ...
    async def verify(self) -> None: ...
    async def commit(self) -> int | None: ...


class NoOpEngine:
    """Does nothing. The stages are real, the work is not yet."""

    async def validate(self) -> None: ...
    async def apply(self) -> None: ...
    async def verify(self) -> None: ...
    async def commit(self) -> int | None:
        return None


class ApplyRunner:
    """Drives an Apply through its stages, in the background."""

    def __init__(self, store: ApplyStore, engine: ApplyEngine) -> None:
        self._store = store
        self._engine = engine
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
        steps: tuple[tuple[Stage, Callable[[], Awaitable[Any]]], ...] = (
            (Stage.VALIDATING, self._engine.validate),
            (Stage.APPLYING, self._engine.apply),
            (Stage.VERIFYING, self._engine.verify),
            (Stage.COMMITTING, self._engine.commit),
        )
        try:
            for index, (stage, step) in enumerate(steps):
                if index:  # the first stage is recorded at creation
                    await self._store.advance(apply_id, stage)
                logger.info("apply stage", extra={"stage": stage.value})
                await step()
            await self._store.advance(apply_id, Stage.SUCCEEDED)
            logger.info("apply succeeded")
        except Exception as exc:
            logger.exception("apply failed")
            await self._store.fail(apply_id, f"{type(exc).__name__}: {exc}")
        finally:
            apply_id_var.reset(token)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()


async def recover_interrupted(store: ApplyStore) -> list[str]:
    """Resolve Applies left in flight by a restart.

    The Candidate is frozen for the whole of an Apply, so an Apchi crash mid-flight
    would freeze it permanently. Whatever else recovery decides, the Candidate must
    end up unfrozen -- that is the part that must never be left to chance.
    """
    recovered: list[str] = []
    while (record := await store.in_flight()) is not None:
        await store.fail(
            record.id,
            "Apchi restarted while this Apply was in flight; it was not completed.",
        )
        logger.warning("recovered interrupted apply", extra={"recovered_apply": record.id})
        recovered.append(record.id)
    return recovered

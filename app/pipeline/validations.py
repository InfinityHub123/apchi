"""The Validate action as a resource.

Section 9: a separate Validate action lets Operators test a Candidate without
applying it. Bringing up a coordinator takes as long as it takes, so this is a
resource with an identifier rather than a request that blocks.

It runs the Apply engine's `validate` and stops there. That is the whole point --
Validation is defined as the stage that touches nothing -- and it means the Validate
action cannot drift from the Validation an Apply performs, because it is the same
code path.
"""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field
from pymongo.asynchronous.database import AsyncDatabase

from app.logging import apply_id_var
from app.pipeline.applies import EngineFactory
from app.pipeline.validation import ValidationFailed, ValidationFailure

logger = logging.getLogger(__name__)


class Outcome(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


class ValidationRecord(BaseModel):
    id: str
    outcome: Outcome
    failures: list[ValidationFailure] = Field(
        default_factory=list,
        description="Every reason the Candidate should not be applied, not just the first.",
    )
    base_snapshot: int | None = None
    started_at: datetime
    finished_at: datetime | None = None


class ValidationStore:
    def __init__(self, database: AsyncDatabase[dict[str, Any]]) -> None:
        self._collection = database["validations"]

    @staticmethod
    def _as_record(document: dict[str, Any]) -> ValidationRecord:
        document.pop("_id", None)
        return ValidationRecord.model_validate(document)

    async def create(self, base_snapshot: int | None) -> ValidationRecord:
        record = ValidationRecord(
            id=f"val_{uuid.uuid4().hex[:16]}",
            outcome=Outcome.RUNNING,
            base_snapshot=base_snapshot,
            started_at=datetime.now(UTC),
        )
        await self._collection.insert_one({"_id": record.id, **record.model_dump(mode="json")})
        return record

    async def get(self, validation_id: str) -> ValidationRecord | None:
        document = await self._collection.find_one({"_id": validation_id})
        return None if document is None else self._as_record(document)

    async def list(self, limit: int = 50) -> list[ValidationRecord]:
        cursor = self._collection.find().sort("started_at", -1).limit(limit)
        return [self._as_record(document) async for document in cursor]

    async def finish(
        self, validation_id: str, outcome: Outcome, failures: Sequence[ValidationFailure]
    ) -> None:
        await self._collection.update_one(
            {"_id": validation_id},
            {
                "$set": {
                    "outcome": outcome.value,
                    "failures": [failure.model_dump(mode="json") for failure in failures],
                    "finished_at": datetime.now(UTC).isoformat(),
                }
            },
        )


class ValidationRunner:
    """Runs a Validation in the background.

    A Validation does not freeze the Candidate: it changes nothing, so there is
    nothing for a concurrent edit to corrupt. The Candidate it reports on is the one
    that existed when it started, which the record's base_snapshot pins down.
    """

    def __init__(self, store: ValidationStore, engine_factory: EngineFactory) -> None:
        self._store = store
        self._engine_factory = engine_factory
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, record: ValidationRecord) -> None:
        task = asyncio.create_task(self._run(record.id))
        # Hold a reference: a bare create_task may be garbage collected mid-flight.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, validation_id: str) -> None:
        token = apply_id_var.set(validation_id)
        try:
            await self._engine_factory(validation_id).validate()
        except ValidationFailed as exc:
            logger.info("validation failed", extra={"failures": len(exc.failures)})
            await self._store.finish(validation_id, Outcome.FAILED, exc.failures)
        except Exception as exc:
            # Validation could not reach a verdict. That is a failure of Apchi, not
            # of the Candidate, and it must not be reported as a clean pass.
            logger.exception("validation could not run")
            await self._store.finish(
                validation_id,
                Outcome.FAILED,
                [
                    ValidationFailure(
                        reason=f"Validation could not run: {type(exc).__name__}: {exc}"
                    )
                ],
            )
        else:
            await self._store.finish(validation_id, Outcome.PASSED, [])
            logger.info("validation passed")
        finally:
            apply_id_var.reset(token)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()

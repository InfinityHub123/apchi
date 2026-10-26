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

from app.api.pages import (
    DEFAULT_LIMIT,
    BadCursor,
    Page,
    after,
    bounded,
    decode_cursor,
    encode_cursor,
)
from app.logging import apply_id_var
from app.pipeline.applies import EngineFactory
from app.pipeline.validation import ValidationFailed
from app.sections.base import ValidationFailure

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
    warnings: list[ValidationFailure] = Field(
        default_factory=list,
        description=(
            "Things worth knowing that did not stop the Apply. A Catalog the validation "
            "coordinator could not connect to is the one case today: `CREATE CATALOG` "
            "succeeds against a host that does not resolve, so this is the only stage that "
            "would notice -- and the probe's network is not the coordinator's, so it "
            "reports rather than refuses. `catalog_connectivity_blocks_apply` moves these "
            "into `failures` on a deployment where the two are known to match."
        ),
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

    async def page(
        self, limit: int = DEFAULT_LIMIT, before: str | None = None
    ) -> Page[ValidationRecord]:
        """Newest first, by when it started.

        `started_at` is not unique -- two can begin in the same instant -- so the cursor
        carries the id beside it and the boundary stays exact.
        """
        limit = bounded(limit)
        query: dict[str, Any] = {}
        if before is not None:
            at, identifier = _started_in(before)
            query.update(after("started_at", at, identifier))
        cursor = (
            self._collection.find(query).sort([("started_at", -1), ("_id", -1)]).limit(limit + 1)
        )
        documents = [document async for document in cursor]
        items = [self._as_record(document) for document in documents[:limit]]
        more = len(documents) > limit
        return Page(
            items=items,
            next=(
                encode_cursor(items[-1].started_at.isoformat(), items[-1].id)
                if more and items
                else None
            ),
        )

    async def finish(
        self,
        validation_id: str,
        outcome: Outcome,
        failures: Sequence[ValidationFailure],
        warnings: Sequence[ValidationFailure] = (),
    ) -> None:
        await self._collection.update_one(
            {"_id": validation_id},
            {
                "$set": {
                    "outcome": outcome.value,
                    "failures": [failure.model_dump(mode="json") for failure in failures],
                    "warnings": [warning.model_dump(mode="json") for warning in warnings],
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
        engine = self._engine_factory(validation_id)
        try:
            await engine.validate()
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
            # A Validation that passed with something to say still passed. The warnings ride
            # on the record rather than changing the outcome, because an outcome a client has
            # to interpret is one that will be interpreted wrongly somewhere.
            warnings = engine.warnings()
            await self._store.finish(validation_id, Outcome.PASSED, [], warnings)
            logger.info("validation passed", extra={"warnings": len(warnings)})
        finally:
            apply_id_var.reset(token)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()


def _started_in(cursor: str) -> tuple[datetime, str]:
    """The instant and the id a cursor names."""
    at, identifier = decode_cursor(cursor, expected=2)
    try:
        return datetime.fromisoformat(at), identifier
    except ValueError as bad:
        raise BadCursor(cursor) from bad

"""Snapshots: the history of Operator-managed configuration.

A Snapshot is immutable, numbered sequentially, and created only after its
configuration passed validation, was applied to the actual Cluster, and passed
verification. That is what makes a Snapshot safe to return to.

A Snapshot records Operator-managed configuration, not the whole Cluster: what ran
was the Snapshot merged with the Admin values current at the time. Restoring one
later merges it with today's Admin values, a combination never verified together.
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator
from pymongo.asynchronous.database import AsyncDatabase

from app.api.errors import Conflict
from app.api.pages import (
    DEFAULT_LIMIT,
    BadCursor,
    Page,
    bounded,
    decode_cursor,
    encode_cursor,
)
from app.sections import SectionName, without_retired
from app.sections.registry import SECTIONS


class Snapshot(BaseModel):
    number: int = Field(description="Sequential within this Cluster.")
    sections: dict[SectionName, dict[str, Any]]
    created_at: datetime

    _drop_retired = field_validator("sections", mode="before")(lambda value: without_retired(value))
    apply_id: str = Field(description="The Apply that produced this Snapshot.")


class SnapshotMissing(Conflict):
    """A Snapshot something refers to is no longer in MongoDB.

    Snapshots are never deleted by Apchi, so this is one removed by hand. Read as an empty
    baseline instead -- which is what happened before -- every resource the Candidate holds
    would be planned as new, Review would show the whole configuration as added, and an Apply
    would issue DDL for catalogs that already exist.
    """


class SnapshotStore:
    def __init__(self, database: AsyncDatabase[dict[str, Any]]) -> None:
        self._collection = database["snapshots"]
        #: The highest number ever committed, kept apart from the Snapshots themselves so that
        #: deleting the latest one by hand cannot make the next commit reuse its number -- and
        #: every Apply record and audit event that named it point at a different configuration.
        self._counters = database["counters"]
        self._applies = database["applies"]

    @staticmethod
    def _as_snapshot(document: dict[str, Any]) -> Snapshot:
        document.pop("_id", None)
        return Snapshot.model_validate(document)

    async def latest(self) -> Snapshot | None:
        document = await self._collection.find_one(sort=[("number", -1)])
        return None if document is None else self._as_snapshot(document)

    async def retired_section(self, name: str) -> dict[str, Any]:
        """What the latest Snapshot holds of a Section that no longer exists.

        Read from the stored document, because the model drops retired Sections on the way in
        -- which is what lets every other reader ignore them.
        """
        document = await self._collection.find_one(sort=[("number", -1)])
        if document is None:
            return {}
        return dict((document.get("sections") or {}).get(name) or {})

    async def get(self, number: int) -> Snapshot | None:
        document = await self._collection.find_one({"_id": number})
        return None if document is None else self._as_snapshot(document)

    async def page(self, limit: int = DEFAULT_LIMIT, before: str | None = None) -> Page[Snapshot]:
        """Newest first, by Snapshot number.

        The number is sequential and unique, so it is the whole sort key -- but the cursor
        still carries the `_id` beside it, because `_id` *is* the number here and keeping the
        shape identical across collections is what §19 asked for.
        """
        limit = bounded(limit)
        query: dict[str, Any] = {}
        if before is not None:
            query["number"] = {"$lt": _number_in(before)}
        cursor = self._collection.find(query).sort("number", -1).limit(limit + 1)
        documents = [document async for document in cursor]
        items = [self._as_snapshot(document) for document in documents[:limit]]
        more = len(documents) > limit
        return Page(
            items=items,
            next=encode_cursor(str(items[-1].number)) if more and items else None,
        )

    async def sections_of(self, number: int | None) -> dict[SectionName, dict[str, Any]]:
        """The configuration a Snapshot holds, or an empty baseline before the first.

        Raises SnapshotMissing for a number that was committed and is gone: "before the first"
        is `None`, and a number is never that.
        """
        empty: dict[SectionName, dict[str, Any]] = {name: {} for name in SECTIONS}
        if number is None:
            return empty
        snapshot = await self.get(number)
        if snapshot is None:
            raise SnapshotMissing(
                f"Snapshot {number} is no longer in MongoDB, and the configuration was "
                "derived from it. Compared with nothing, every resource would read as new and "
                "an Apply would recreate what the Cluster already has. Re-derive the "
                "Candidate from the latest Snapshot with POST /api/v1/candidate/reset -- it "
                "discards the staged changes."
            )
        return {name: snapshot.sections.get(name, {}) for name in SECTIONS}

    async def _next_number(self) -> int:
        """One past the highest number ever committed.

        The highest of three records, because any of them can be the only one left: the
        latest Snapshot, the counter, and the Apply records, which name every Snapshot they
        committed -- so even an install from before the counter, whose latest Snapshot was
        deleted, does not reuse a number.
        """
        latest = await self.latest()
        counter = await self._counters.find_one({"_id": "snapshots"})
        recorded = await self._applies.find_one(
            {"snapshot": {"$ne": None}}, sort=[("snapshot", -1)], projection={"snapshot": 1}
        )
        highest = max(
            0 if latest is None else latest.number,
            0 if counter is None else int(counter["last"]),
            0 if recorded is None else int(recorded["snapshot"]),
        )
        return highest + 1

    async def commit(self, sections: dict[SectionName, dict[str, Any]], apply_id: str) -> Snapshot:
        """Records a verified Candidate as the next Snapshot.

        Immutability is enforced by never updating a Snapshot document: the only
        write is this insert.
        """
        snapshot = Snapshot(
            number=await self._next_number(),
            sections=sections,
            created_at=datetime.now(UTC),
            apply_id=apply_id,
        )
        await self._collection.insert_one(
            {"_id": snapshot.number, **snapshot.model_dump(mode="json")}
        )
        await self._counters.update_one(
            {"_id": "snapshots"}, {"$max": {"last": snapshot.number}}, upsert=True
        )
        return snapshot


def _number_in(cursor: str) -> int:
    """A snapshot cursor is one number. Anything else is a cursor from somewhere else."""
    (number,) = decode_cursor(cursor, expected=1)
    try:
        return int(number)
    except ValueError as bad:
        raise BadCursor(cursor) from bad

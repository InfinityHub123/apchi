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

from pydantic import BaseModel, Field
from pymongo.asynchronous.database import AsyncDatabase

from app.sections import SectionName
from app.sections.registry import SECTIONS


class Snapshot(BaseModel):
    number: int = Field(description="Sequential within this Cluster.")
    sections: dict[SectionName, dict[str, Any]]
    created_at: datetime
    apply_id: str = Field(description="The Apply that produced this Snapshot.")


class SnapshotStore:
    def __init__(self, database: AsyncDatabase[dict[str, Any]]) -> None:
        self._collection = database["snapshots"]

    @staticmethod
    def _as_snapshot(document: dict[str, Any]) -> Snapshot:
        document.pop("_id", None)
        return Snapshot.model_validate(document)

    async def latest(self) -> Snapshot | None:
        document = await self._collection.find_one(sort=[("number", -1)])
        return None if document is None else self._as_snapshot(document)

    async def get(self, number: int) -> Snapshot | None:
        document = await self._collection.find_one({"_id": number})
        return None if document is None else self._as_snapshot(document)

    async def list(self, limit: int = 50) -> list[Snapshot]:
        cursor = self._collection.find().sort("number", -1).limit(limit)
        return [self._as_snapshot(document) async for document in cursor]

    async def sections_of(self, number: int | None) -> dict[SectionName, dict[str, Any]]:
        """The configuration a Snapshot holds, or an empty baseline before the first."""
        empty: dict[SectionName, dict[str, Any]] = {name: {} for name in SECTIONS}
        if number is None:
            return empty
        snapshot = await self.get(number)
        if snapshot is None:
            return empty
        return {name: snapshot.sections.get(name, {}) for name in SECTIONS}

    async def commit(self, sections: dict[SectionName, dict[str, Any]], apply_id: str) -> Snapshot:
        """Records a verified Candidate as the next Snapshot.

        Immutability is enforced by never updating a Snapshot document: the only
        write is this insert.
        """
        previous = await self.latest()
        snapshot = Snapshot(
            number=1 if previous is None else previous.number + 1,
            sections=sections,
            created_at=datetime.now(UTC),
            apply_id=apply_id,
        )
        await self._collection.insert_one(
            {"_id": snapshot.number, **snapshot.model_dump(mode="json")}
        )
        return snapshot

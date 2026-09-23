"""The Configuration Candidate: one per Cluster, derived from the latest Snapshot.

There is no transaction object and no identifier on mutating requests. Every
Operator change lands here, and nothing reaches the Cluster until Apply.

The Candidate is shared, which is a deliberate trade (ADR-0002). Two consequences
shape this module: an invalid resource would block everyone's Apply, so static
validation rejects before anything is stored; and the audit of who changed what is
the only record of whose work an Apply is shipping.
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from pymongo.asynchronous.database import AsyncDatabase

from app.pipeline.snapshots import SnapshotStore
from app.sections import SectionName
from app.sections.registry import SECTIONS

# One Cluster per Apchi deployment, so the Candidate is a singleton document.
CANDIDATE_ID = "candidate"


class Candidate(BaseModel):
    """The mutable configuration Operators edit."""

    base_snapshot: int | None = Field(
        default=None,
        description="The Snapshot this Candidate was derived from; null before the first.",
    )
    sections: dict[SectionName, dict[str, Any]] = Field(default_factory=dict)
    updated_at: datetime | None = None

    def resources(self, section: SectionName) -> dict[str, Any]:
        return self.sections.setdefault(section, {})


class CandidateStore:
    """Persistence for the Candidate. Nothing here touches Trino or Kubernetes.

    A Candidate is by definition *derived from* the latest Snapshot, so this needs
    the Snapshot store: a fresh or reset Candidate carries that Snapshot's
    configuration rather than being empty. An empty Candidate derived from a
    non-empty Snapshot would read as "remove everything".
    """

    def __init__(self, database: AsyncDatabase[dict[str, Any]], snapshots: SnapshotStore) -> None:
        self._collection = database["candidate"]
        self._snapshots = snapshots

    async def load(self) -> Candidate:
        document = await self._collection.find_one({"_id": CANDIDATE_ID})
        if document is None:
            return await self._derive_from_latest()
        document.pop("_id", None)
        candidate = Candidate.model_validate(document)
        for name in SECTIONS:
            candidate.sections.setdefault(name, {})
        return candidate

    async def _derive_from_latest(self) -> Candidate:
        latest = await self._snapshots.latest()
        number = None if latest is None else latest.number
        return Candidate(
            base_snapshot=number,
            sections=await self._snapshots.sections_of(number),
        )

    async def save(self, candidate: Candidate) -> None:
        candidate.updated_at = datetime.now(UTC)
        await self._collection.replace_one(
            {"_id": CANDIDATE_ID},
            {"_id": CANDIDATE_ID, **candidate.model_dump(mode="json")},
            upsert=True,
        )

    async def reset(self, base_snapshot: int | None = None) -> Candidate:
        """Discard the Candidate's changes and re-derive it from a Snapshot.

        Re-derived means it carries that Snapshot's configuration, so its diff is
        empty -- not that it is emptied. Because nothing reaches the Cluster before
        Apply, this leaves the Effective Cluster State untouched.
        """
        if base_snapshot is None:
            fresh = await self._derive_from_latest()
        else:
            fresh = Candidate(
                base_snapshot=base_snapshot,
                sections=await self._snapshots.sections_of(base_snapshot),
            )
        await self.save(fresh)
        return fresh

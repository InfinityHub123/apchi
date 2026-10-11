"""Where a discovery lives between being read and being adopted.

Adoption is not a button. Apchi reads the Cluster, and what it could not read is the
Operator's to supply -- chiefly the properties of a Catalog created by DDL, which exist only
in the coordinator's writable store and which Apchi will not invent (#88). On a Cluster with
forty catalogs that is not work anyone finishes in one sitting, so the document persists and
the amendments persist with it.

Which makes re-reading the dangerous operation, and the reason this module exists rather than
the API calling `discover` twice: an Operator who typed twenty connection strings and then
re-read the Cluster must not lose them. A reading and the amendments to it are kept apart so
the reading can be replaced without touching them.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from pymongo.asynchronous.database import AsyncDatabase

from app.pipeline.discovery import Discovery, Problem
from app.sections import SectionName
from app.sections.base import Resources

logger = logging.getLogger(__name__)

#: One document. Adoption runs once per Cluster (#91 refuses it after the first Snapshot), so
#: a second concurrent discovery would be two answers to a question with one answer.
_DOCUMENT_ID = "discovery"


class Amendment(BaseModel):
    """One thing an Operator supplied that Apchi could not read."""

    section: SectionName
    resource: str
    value: dict[str, Any]
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StoredDiscovery(BaseModel):
    """A reading of the Cluster, and what an Operator has added to it."""

    read: Discovery
    read_at: datetime
    #: Keyed by Section and then resource, so re-reading can replace the reading wholesale
    #: without touching them.
    amendments: dict[SectionName, dict[str, Amendment]] = Field(default_factory=dict)
    #: What changed in the Cluster between the previous reading and this one. Empty on a
    #: first read. An Operator part-way through completing a discovery needs to know the
    #: ground moved, and needs it said rather than silently merged.
    changed_since_last_read: list[str] = Field(default_factory=list)

    def resolved(self, section: SectionName) -> Resources:
        """What that Section holds once the amendments are applied.

        Amendments win. They exist precisely because the reading was incomplete, and an
        Operator correcting a misread value is the other reason to supply one.
        """
        found = next((s for s in self.read.sections if s.section == section), None)
        resources: Resources = dict(found.resources) if found else {}
        for name, amendment in self.amendments.get(section, {}).items():
            resources[name] = amendment.value
        return resources

    @property
    def outstanding(self) -> list[Problem]:
        """Problems an amendment has not answered.

        A `cutover` problem is never outstanding: it names something the deployment must
        change after adoption, not something an Operator can fix now (§82's amendment). An
        `incomplete` catalog stops being outstanding the moment its properties are supplied,
        which is the whole point of amendments.
        """
        answered = {
            (section, name)
            for section, amendments in self.amendments.items()
            for name in amendments
        }
        return [
            problem
            for problem in self.read.every_problem
            if problem.kind != "cutover" and (problem.section, problem.path) not in answered
        ]

    @property
    def complete(self) -> bool:
        """Whether this discovery is ready to seed and adopt. What #101 and #91 gate on."""
        return not self.outstanding


def differences(previous: Discovery | None, current: Discovery) -> list[str]:
    """What the Cluster changed between two readings, in an Operator's terms.

    Said rather than merged. A discovery being completed over days is a discovery whose
    subject can move underneath it, and an Operator who supplied a catalog's properties on
    Monday should hear that the catalog is gone on Thursday rather than adopt a Cluster that
    no longer looks like the one they read.
    """
    if previous is None:
        return []
    changes: list[str] = []
    before = {section.section: section for section in previous.sections}
    for section in current.sections:
        was = before.get(section.section)
        if was is None:
            continue
        gone = sorted(set(was.resources) - set(section.resources))
        arrived = sorted(set(section.resources) - set(was.resources))
        altered = sorted(
            name
            for name, value in section.resources.items()
            if name in was.resources and was.resources[name] != value
        )
        for name in gone:
            changes.append(f"{section.section}: {name} is no longer on the Cluster")
        for name in arrived:
            changes.append(f"{section.section}: {name} is new on the Cluster")
        for name in altered:
            changes.append(f"{section.section}: {name} has changed on the Cluster")
    return changes


class DiscoveryStore:
    def __init__(self, database: AsyncDatabase[dict[str, Any]]) -> None:
        self._collection = database["discoveries"]

    async def load(self) -> StoredDiscovery | None:
        document = await self._collection.find_one({"_id": _DOCUMENT_ID})
        if document is None:
            return None
        document.pop("_id", None)
        return StoredDiscovery.model_validate(document)

    async def record(self, read: Discovery) -> StoredDiscovery:
        """Store a new reading, keeping whatever has been amended.

        The amendments are carried across deliberately. Dropping them would make re-reading
        a destructive operation, and an Operator re-reads precisely when they are unsure --
        which is the worst moment to take their work away.
        """
        existing = await self.load()
        stored = StoredDiscovery(
            read=read,
            read_at=datetime.now(UTC),
            amendments=existing.amendments if existing else {},
            changed_since_last_read=differences(existing.read if existing else None, read),
        )
        await self._save(stored)
        logger.info(
            "discovery recorded",
            extra={
                "outstanding": len(stored.outstanding),
                "amendments": sum(len(a) for a in stored.amendments.values()),
                "changed": len(stored.changed_since_last_read),
            },
        )
        return stored

    async def amend(
        self, section: SectionName, resource: str, value: dict[str, Any]
    ) -> StoredDiscovery:
        stored = await self.load()
        if stored is None:
            raise LookupError("there is no discovery to amend")
        stored.amendments.setdefault(section, {})[resource] = Amendment(
            section=section, resource=resource, value=value
        )
        await self._save(stored)
        logger.info("discovery amended", extra={"section": section, "resource": resource})
        return stored

    async def withdraw(self, section: SectionName, resource: str) -> StoredDiscovery:
        stored = await self.load()
        if stored is None:
            raise LookupError("there is no discovery to amend")
        amendments = stored.amendments.get(section, {})
        if resource not in amendments:
            raise KeyError(resource)
        del amendments[resource]
        await self._save(stored)
        return stored

    async def discard(self) -> None:
        await self._collection.delete_one({"_id": _DOCUMENT_ID})
        logger.info("discovery discarded")

    async def _save(self, stored: StoredDiscovery) -> None:
        await self._collection.replace_one(
            {"_id": _DOCUMENT_ID},
            {"_id": _DOCUMENT_ID, **stored.model_dump(mode="json")},
            upsert=True,
        )

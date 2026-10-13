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
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.audit.actor import actor
from app.audit.changes import Sections, changes_between
from app.audit.events import Action, AuditStore, Category, Entry
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

    def __init__(
        self,
        database: AsyncDatabase[dict[str, Any]],
        snapshots: SnapshotStore,
        audit: AuditStore,
    ) -> None:
        self._collection = database["candidate"]
        self._snapshots = snapshots
        self._audit = audit

    async def load(self) -> Candidate:
        document = await self._collection.find_one({"_id": CANDIDATE_ID})
        if document is None:
            return await self._derive_from_latest()
        document.pop("_id", None)
        candidate = Candidate.model_validate(document)
        for name in SECTIONS:
            candidate.sections.setdefault(name, {})
        return candidate

    async def stored(self) -> Candidate | None:
        """What the collection actually holds, or None when it holds nothing.

        `load` answers "what are Operators editing", deriving a Candidate when none is
        stored. An export asks the narrower question -- what would be lost -- and a
        derived Candidate would not be lost: it comes back from the Snapshot it was
        derived from, which the export already holds (#113).
        """
        document = await self._collection.find_one({"_id": CANDIDATE_ID})
        if document is None:
            return None
        document.pop("_id", None)
        return Candidate.model_validate(document)

    async def _derive_from_latest(self) -> Candidate:
        latest = await self._snapshots.latest()
        number = None if latest is None else latest.number
        return Candidate(
            base_snapshot=number,
            sections=await self._snapshots.sections_of(number),
        )

    async def save(
        self,
        candidate: Candidate,
        *,
        category: Category | None = None,
        action: Action | None = None,
        detail: str | None = None,
    ) -> None:
        """Store the Candidate, and record who changed what (§20).

        **This is where the audit of Operator work is written**, because this is the one
        function every Operator edit reaches: create, update, delete, Section Revert, Reset
        and Import all end here. Recording in each of the routes instead would be about forty
        places to remember, and the first one anybody forgot would be a change with no actor
        that still worked -- the same argument that made `operator_mutation_allowed` one
        dependency rather than three a route could pick from.

        With no `category`, the events are **derived** from the difference between what was
        stored and what is being stored: one per resource added, removed or edited. A route
        therefore never describes its own change, so it cannot describe it wrongly, and a
        Section added later is audited without anything here changing.

        `category` and `action` together override that, for the operations whose shape is not
        a set of resource edits: a Reset or an Import is one act on the whole Candidate, and a
        Section Revert is one act on one Section. Deriving those would record a plausible
        list of resource changes and lose the only thing somebody reading the trail wants to
        know, which is that it was a revert.

        `detail` carries the Snapshot such an act restored *from*. Not the `snapshot` field:
        that one means the Snapshot an Apply **produced** (§20), and a staged act has produced
        none -- putting a source number there would read as a commit that never happened.
        """
        candidate.updated_at = datetime.now(UTC)
        dumped = candidate.model_dump(mode="json")
        # `find_one_and_replace` rather than a read followed by a `replace_one`: the diff needs
        # the pre-image, and this returns it *from the write itself*. One round trip instead of
        # two, and the pre-image is the document this call replaced rather than one another
        # request may have changed in between.
        #
        # Round trips are not an abstract concern here: tier 1 starts an app per test and the
        # suite wedges its MongoDB when per-operation traffic grows (#69), which is how this
        # version came to be measured rather than assumed.
        previous = await self._collection.find_one_and_replace(
            {"_id": CANDIDATE_ID},
            {"_id": CANDIDATE_ID, **dumped},
            upsert=True,
            return_document=ReturnDocument.BEFORE,
        )
        # After the write, never before. An event naming a change that then failed to store is
        # worse than no event: it attributes something to somebody that never happened.
        await self._record(_sections_of(previous), dumped["sections"], category, action, detail)

    async def _record(
        self,
        before: Sections,
        after: Sections,
        category: Category | None,
        action: Action | None,
        detail: str | None,
    ) -> None:
        who = actor()
        if category is not None and action is not None:
            await self._audit.record(
                who.subject, who.via, category, action, detail=detail, token_id=who.token_id
            )
            return
        # One write for however many resources moved. A request that edited five of them is
        # one act by one person, and five round trips for it is five times the traffic for no
        # extra information.
        await self._audit.record_each(
            who.subject,
            who.via,
            [
                Entry(category=change.category, action=change.action, resource=change.resource)
                for change in changes_between(before, after)
            ],
            token_id=who.token_id,
        )

    async def replace(self, sections: dict[SectionName, dict[str, Any]]) -> Candidate:
        """Replace the Candidate wholesale with an imported configuration (#121).

        `base_snapshot` is **this** Cluster's latest Snapshot, never anything the document
        says. A Candidate's base is what Review diffs against, and Review has to keep
        meaning "what would applying this change here": a base taken from the document
        would diff an imported configuration against a Cluster this one is not, and report
        a change nobody is about to make.

        Distinct from `reset`, which re-derives from a Snapshot and so always produces an
        empty diff. This one is expected to produce a large one -- that is what importing
        somebody else's configuration looks like.
        """
        latest = await self._snapshots.latest()
        fresh = Candidate(
            base_snapshot=None if latest is None else latest.number, sections=sections
        )
        await self.save(fresh, category="pipeline", action="imported")
        return fresh

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
        await self.save(
            fresh,
            category="pipeline",
            action="reset",
            detail=_from_snapshot(fresh.base_snapshot),
        )
        return fresh


def _sections_of(document: dict[str, Any] | None) -> Sections:
    """A stored Candidate's Sections, dumped by the same model as the incoming one.

    Normalised through `Candidate` rather than compared raw, because MongoDB returns BSON -- a
    datetime comes back as a datetime where the incoming side has an ISO string -- and
    comparing those two directly would report every resource as edited on every save.
    """
    if document is None:
        return {}
    document.pop("_id", None)
    dumped: Sections = Candidate.model_validate(document).model_dump(mode="json")["sections"]
    return dumped


def _from_snapshot(number: int | None) -> str:
    """Which Snapshot a staged act took its content from, in words.

    None is the pre-first-Snapshot case, which is a real state rather than missing data: a
    Reset on a Cluster that has never committed anything re-derives an empty baseline.
    """
    return (
        "restored from an empty baseline" if number is None else f"restored from Snapshot {number}"
    )

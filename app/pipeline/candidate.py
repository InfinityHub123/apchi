"""The Configuration Candidate: one per Cluster, derived from the latest Snapshot.

There is no transaction object and no identifier on mutating requests. Every
Operator change lands here, and nothing reaches the Cluster until Apply.

The Candidate is shared, which is a deliberate trade (ADR-0002). Two consequences
shape this module: an invalid resource would block everyone's Apply, so static
validation rejects before anything is stored; and the audit of who changed what is
the only record of whose work an Apply is shipping.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from bson import encode as bson_encode
from pydantic import BaseModel, Field, field_validator
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.api.errors import CandidateTooLarge, NotFound
from app.audit.actor import actor
from app.audit.changes import Sections, changes_between
from app.audit.events import Action, AuditStore, Category, Entry
from app.pipeline.snapshots import SnapshotStore
from app.sections import SectionName, without_retired
from app.sections.registry import SECTIONS

logger = logging.getLogger(__name__)

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

    _drop_retired = field_validator("sections", mode="before")(lambda value: without_retired(value))

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
        max_bytes: int = 15 * 1024 * 1024,
    ) -> None:
        self._collection = database["candidate"]
        self._snapshots = snapshots
        self._audit = audit
        self._max_bytes = max_bytes

    async def load(self) -> Candidate:
        document = await self._collection.find_one({"_id": CANDIDATE_ID})
        if document is None:
            return await self._derive_from_latest()
        document.pop("_id", None)
        candidate = Candidate.model_validate(document)
        for name in SECTIONS:
            candidate.sections.setdefault(name, {})
        return candidate

    @property
    def max_bytes(self) -> int:
        """The ceiling a Candidate is refused at, so a client can say how close it is.

        Below MongoDB's 16MB document limit deliberately (see `_refuse_if_too_large`), which
        is why it is this number and not that one: an interface warning at the BSON limit
        would warn after Apchi had already started refusing edits.
        """
        return self._max_bytes

    def size_of(self, candidate: Candidate) -> int:
        """How large this Candidate is, measured the way the ceiling is measured.

        BSON of the document as it would be stored, `_id` included, because that is what the
        limit applies to -- the JSON text is a different number by enough to matter at this
        size, and reporting it would put the warning in the wrong place.
        """
        return len(bson_encode({"_id": CANDIDATE_ID, **candidate.model_dump(mode="json")}))

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
        resource: str | None = None,
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

        `resource` names what such an act was about, when it was about one thing: a Reset of a
        single resource is still a Reset, and recording it without the resource would make it
        indistinguishable in the trail from discarding everything.

        `detail` carries the Snapshot such an act restored *from*. Not the `snapshot` field:
        that one means the Snapshot an Apply **produced** (§20), and a staged act has produced
        none -- putting a source number there would read as a commit that never happened.
        """
        candidate.updated_at = datetime.now(UTC)
        dumped = candidate.model_dump(mode="json")
        # Before the write, so a Candidate that would not fit is refused with the stored one
        # untouched. A 422 naming what grew, rather than the BSON failure an unguarded
        # replace_one would raise on whichever edit happened to cross the line (#149).
        _refuse_if_too_large(dumped, self._max_bytes)
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
        await self._record(
            _sections_of(previous), dumped["sections"], category, action, detail, resource
        )

    async def _record(
        self,
        before: Sections,
        after: Sections,
        category: Category | None,
        action: Action | None,
        detail: str | None,
        resource: str | None = None,
    ) -> None:
        who = actor()
        if category is not None and action is not None:
            await self._audit.record(
                who.subject,
                who.via,
                category,
                action,
                resource=resource,
                detail=detail,
                token_id=who.token_id,
                token_name=who.token_name,
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
            token_name=who.token_name,
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
        if base_snapshot is None or await self._snapshots.get(base_snapshot) is None:
            # A base that is gone -- deleted from MongoDB by hand -- is what a Reset is the way
            # out of: Review and Apply refuse a Candidate derived from it, and the Snapshot it
            # would re-derive from no longer exists. The latest one is the Cluster's own.
            if base_snapshot is not None:
                logger.warning(
                    "the candidate's base snapshot no longer exists; re-deriving from the latest",
                    extra={"missing_snapshot": base_snapshot},
                )
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

    async def reset_resource(self, section: SectionName, resource: str) -> None:
        """Reset one resource: put it back as the Candidate's base Snapshot has it.

        Reset scoped to a single resource, so the Candidate's other staged changes -- in this
        Section and every other, including other people's -- stay where they are. The
        smallest unit used to be a Section Revert, which discards everyone's edits to that
        Section along with the one being undone.

        "Back as the base Snapshot has it" covers all three shapes of staged change: an
        edited resource gets the Snapshot's version, a removed one comes back, and an added
        one is removed. Against the base Snapshot rather than the latest, for the reason
        Review diffs against it -- that is what the Candidate was derived from, and so what
        "staged" is measured against.

        Refused when the resource has nothing staged. A silent success there would read as
        "undone" to somebody who typed the wrong name.
        """
        candidate = await self.load()
        baseline = await self._snapshots.sections_of(candidate.base_snapshot)
        base = baseline.get(section, {})
        staged = candidate.sections.setdefault(section, {})
        if staged.get(resource) == base.get(resource):
            raise NotFound(
                f"{section} has no staged change to {resource!r}: it is already as Snapshot "
                f"{candidate.base_snapshot} has it."
                if candidate.base_snapshot is not None
                else f"{section} has no staged change to {resource!r}."
            )
        if resource in base:
            staged[resource] = base[resource]
        else:
            del staged[resource]
        await self.save(
            candidate,
            category=section,
            action="reset",
            resource=resource,
            detail=_from_snapshot(candidate.base_snapshot),
        )


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


#: MongoDB's hard limit on one document. Here for the error message rather than for the check:
#: what Apchi refuses at is `candidate_max_bytes`, which sits below this deliberately.
BSON_DOCUMENT_LIMIT = 16 * 1024 * 1024


def _refuse_if_too_large(dumped: dict[str, Any], max_bytes: int) -> None:
    """Refuse a Candidate that would not fit in one document.

    The Candidate is one MongoDB document (ADR-0002, §4) and MongoDB caps a document at 16MB,
    so this ceiling is a consequence of that design rather than a policy. Nothing capped the
    *number* of catalogs, rules or certificates -- only the length of individual fields -- so
    before this a Cluster that accumulated enough configuration met `replace_one` failing
    inside a routine edit and reported a 500.

    Measured as BSON, not as JSON text: the limit is on the encoded document, and the two
    differ by enough to matter at this size.

    The message names the largest Section, because "your configuration is too big" is not
    actionable and "permissions is 12.4 MB of it" is.
    """
    encoded = len(bson_encode({"_id": CANDIDATE_ID, **dumped}))
    if encoded <= max_bytes:
        return
    sections: dict[str, Any] = dumped.get("sections") or {}
    largest = max(
        sections,
        key=lambda name: len(bson_encode({"s": sections[name]})),
        default="the Candidate",
    )
    raise CandidateTooLarge(
        f"This change would make the Configuration Candidate {encoded / 1048576:.1f} MB, over "
        f"the {max_bytes / 1048576:.0f} MB limit. The Candidate is one MongoDB document "
        f"(§4) and MongoDB caps a document at {BSON_DOCUMENT_LIMIT // 1048576} MB, so this is "
        f"a ceiling on everything staged together rather than on any one Section. "
        f"{largest!r} is the largest. Remove something before adding more.",
        details=[{"section": largest, "problem": "the Candidate is at its size limit"}],
    )

"""The audit event, and the collection it lives in.

§20 names the fields an event records: actor, time, Section, resource, change, resulting
Snapshot, and outcome. Three of those need a decision this module makes.

**Category, not Section.** §20 is explicit that Admin changes are audited and are *not* a
Section, so a field typed `SectionName` would have nothing to put there -- and the trail would
silently omit the one class of change that alters the Cluster without appearing in any
Snapshot. `admin` and `pipeline` sit alongside the six.

**No values, ever.** An event records which resource changed and never what it changed to.
§12 puts real credentials in Snapshots; a second copy of a connection password or a private
key, in a collection with its own access rules, its own retention and its own export path, is
a second thing to protect and a second thing to get wrong. The resource identity plus the kind
of change answers "who changed what"; the contents are in the Snapshot, under rules somebody
has already argued about.

**Keep forever by default.** Audit rows only grow, and §18 is clear that operating MongoDB is
the platform's job. An audit trail that silently deletes itself is worse than one that grows:
the first question an incident asks is what happened before the window you kept. So there is
no TTL index unless `APCHI_AUDIT_RETENTION_DAYS` is set, and its being unset by default is the
point -- deleting an audit trail should be a decision somebody made, not a default they
inherited.
"""

import base64
import binascii
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from app.auth.identity import Via
from app.sections import SectionName

logger = logging.getLogger(__name__)

#: Where a change landed. The six Sections, plus the two things that are not Sections:
#: `admin` for low-level configuration and the mutation freeze (§3, §14), and `pipeline` for
#: an Apply, which is about the whole Candidate rather than one Section.
Category = SectionName | Literal["admin", "pipeline"]

#: What was done. Past tense, because an event records something that already happened --
#: writing one before the change lands would name an actor for something that never did.
Action = Literal[
    "created",
    "updated",
    "removed",
    "reset",
    "imported",
    "reverted",
    "applied",
    "rolled_back",
    "engaged",
    "released",
    "minted",
    "revoked",
]

#: `refused` is not a failure. It is Apchi working correctly -- a frozen Candidate,
#: Maintenance Mode, a rejected payload -- and it is recorded because "who tried to change
#: this during the freeze" is a question somebody asks.
Outcome = Literal["succeeded", "refused", "failed"]

#: What a change is attributed to when nothing was on the other end of a request: startup
#: recovery, an Auto Rollback. A real name for the same reason `ANONYMOUS` is one -- a record
#: reading "the system reset this" is answerable, where one that lost its actor looks like a
#: bug in the audit.
SYSTEM: Final = "apchi"


class AuditEvent(BaseModel):
    """One thing that happened, attributed to whoever caused it."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Opaque and stable. Also the sort key within one instant.")
    at: datetime
    actor: str = Field(description="An Identity's subject, or 'apchi' when Apchi itself acted.")
    via: Via = Field(
        description=(
            "How the actor authenticated. On the record rather than derivable, because "
            "'jo did this' and 'an automation jo minted did this' are different answers."
        )
    )
    token_id: str | None = Field(
        default=None,
        description=(
            "Which token acted, when `via` is 'token'. Kept because a revoked token in the "
            "trail still has to resolve -- revocation is exactly when somebody reads this."
        ),
    )
    category: Category
    resource: str | None = Field(
        default=None, description="What changed. Null for an act on a whole Section or Cluster."
    )
    action: Action
    snapshot: int | None = Field(
        default=None,
        description=(
            "The Snapshot this produced, when it produced one. Null for a staged edit, and "
            "null for every Admin change: an Admin Apply creates no Snapshot (§14)."
        ),
    )
    outcome: Outcome = "succeeded"
    detail: str | None = Field(
        default=None,
        description="Why it was refused, or how it failed. Never a configuration value.",
    )
    apply_id: str | None = Field(
        default=None,
        description=(
            "The Apply this belongs to, so the trail joins to the logs for the same Apply "
            "rather than being matched up by timestamp."
        ),
    )


class Page(BaseModel):
    """One page of history, newest first."""

    events: list[AuditEvent]
    next: str | None = Field(
        default=None,
        description=(
            "Pass as `before` for the following page. Null when this is the last one. A "
            "cursor rather than an offset: the collection grows at the head, and an offset "
            "pager repeats or skips rows while it is being appended to."
        ),
    )


def _cursor(at: datetime, identifier: str) -> str:
    """Both halves of the sort key, so a page boundary is exact.

    Sorting by time alone would be ambiguous between two events in the same instant -- two
    resources changed in one request is the ordinary case, not a rare one -- and a boundary
    that cannot distinguish them repeats or skips.
    """
    raw = f"{at.isoformat()}|{identifier}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(cursor: str) -> tuple[datetime, ObjectId] | None:
    """None for anything unreadable, so a mangled cursor is a 422 the caller can act on
    rather than a 500."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        at, _, identifier = base64.urlsafe_b64decode(padded).decode().partition("|")
        return datetime.fromisoformat(at), ObjectId(identifier)
    except (binascii.Error, InvalidId, UnicodeDecodeError, ValueError):
        return None


class BadCursor(ValueError):
    """A `before` value that did not come from a previous page."""


class AuditStore:
    """The audit collection.

    Indexes are created when the **first event is written**, not at startup. An index on a
    collection with no rows has no reader, which is the same argument `main.py` already makes
    for the authentication collections -- and here it is load-bearing rather than tidy: three
    `create_index` round trips per process start wedged the test MongoDB outright, because
    tier 1 starts an app per test (#131, #69). Measured, not assumed: the suite went from
    215 tests in 72 seconds to about 19 in five minutes, and mongod stopped answering its own
    local shell.

    Nothing is lost by waiting. A read before the first write has no rows to scan, and the
    index exists before there is any history for its absence to matter to.
    """

    def __init__(
        self, database: AsyncDatabase[dict[str, Any]], retention_days: float | None = None
    ) -> None:
        self._collection = database["audit"]
        self._retention_days = retention_days
        self._indexed = False

    async def record(
        self,
        actor: str,
        via: Via,
        category: Category,
        action: Action,
        *,
        resource: str | None = None,
        snapshot: int | None = None,
        outcome: Outcome = "succeeded",
        detail: str | None = None,
        token_id: str | None = None,
        apply_id: str | None = None,
    ) -> AuditEvent:
        """Append one event. Keyword-only past the first four, because an event with its
        resource and its actor transposed is a wrong record that reads like a right one."""
        await self._indexed_once()
        at = datetime.now(UTC)
        document: dict[str, Any] = {
            "at": at,
            "actor": actor,
            "via": via,
            "token_id": token_id,
            "category": category,
            "resource": resource,
            "action": action,
            "snapshot": snapshot,
            "outcome": outcome,
            "detail": detail,
            "apply_id": apply_id,
        }
        # A copy, because insert_one assigns the generated `_id` *into* the dict it is given,
        # and the model forbids unknown fields -- deliberately, so a later slice cannot
        # quietly add a value-bearing one.
        result = await self._collection.insert_one(dict(document))
        return AuditEvent(id=str(result.inserted_id), **document)

    async def page(
        self,
        *,
        category: Category | None = None,
        actor: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        before: str | None = None,
    ) -> Page:
        """History newest first, filtered and paged.

        The keyset predicate is what makes the boundary exact: everything strictly older than
        the cursor's instant, plus anything in that same instant with a lower identifier.
        """
        query: dict[str, Any] = {}
        if category is not None:
            query["category"] = category
        if actor is not None:
            query["actor"] = actor
        window = {k: v for k, v in (("$gte", since), ("$lte", until)) if v is not None}
        if window:
            query["at"] = window
        if before is not None:
            decoded = _decode(before)
            if decoded is None:
                raise BadCursor(before)
            at, identifier = decoded
            query["$or"] = [{"at": {"$lt": at}}, {"at": at, "_id": {"$lt": identifier}}]

        # One more than asked for, so "is there another page" is answered by what came back
        # rather than by a second count query that can disagree with it.
        cursor = self._collection.find(query).sort([("at", DESCENDING), ("_id", DESCENDING)])
        documents = [document async for document in cursor.limit(limit + 1)]
        events = [self._as_event(document) for document in documents[:limit]]
        more = len(documents) > limit
        return Page(
            events=events,
            next=_cursor(events[-1].at, events[-1].id) if more and events else None,
        )

    async def _indexed_once(self) -> None:
        """Once per process, before the first write.

        The flag is set only on success, so a MongoDB that was unreachable is retried rather
        than remembered as done -- and `create_index` is idempotent, so a retry costs a round
        trip and nothing else.
        """
        if self._indexed:
            return
        await self.ensure_indexes(self._retention_days)
        self._indexed = True

    @staticmethod
    def _as_event(document: dict[str, Any]) -> AuditEvent:
        identifier = document.pop("_id")
        at = document["at"]
        if at.tzinfo is None:
            # MongoDB returns naive UTC datetimes.
            document["at"] = at.replace(tzinfo=UTC)
        return AuditEvent(id=str(identifier), **document)

    async def ensure_indexes(self, retention_days: float | None = None) -> None:
        """The indexes the endpoint's three queries actually make, and nothing else.

        Every query sorts by `(at, _id)` descending, so each filter gets that pair as its
        suffix: MongoDB then satisfies the match and the sort from one index, with no blocking
        sort to grow with the collection.
        """
        await self._collection.create_index([("at", DESCENDING), ("_id", DESCENDING)])
        await self._collection.create_index(
            [("actor", ASCENDING), ("at", DESCENDING), ("_id", DESCENDING)]
        )
        await self._collection.create_index(
            [("category", ASCENDING), ("at", DESCENDING), ("_id", DESCENDING)]
        )
        if retention_days is not None:
            # Deliberately conditional. See the module docstring: an audit trail that deletes
            # itself by default is worse than one that grows, so this index exists only
            # because somebody chose it.
            await self._collection.create_index(
                "at", expireAfterSeconds=int(timedelta(days=retention_days).total_seconds())
            )
            logger.warning(
                "audit history will be deleted after a retention period",
                extra={"retention_days": retention_days},
            )

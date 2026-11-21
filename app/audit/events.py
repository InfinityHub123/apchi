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

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final, Literal

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from app.api.pages import BadCursor, Page, after, bounded, decode_cursor, encode_cursor
from app.auth.identity import Via
from app.sections import SectionName

logger = logging.getLogger(__name__)

#: Where a change landed. The six Sections, plus the three things that are not Sections:
#: `admin` for low-level configuration and the mutation freeze (§3, §14), `pipeline` for
#: an Apply, which is about the whole Candidate rather than one Section, and `audit` for an
#: act on the trail itself.
#:
#: That last one changes no configuration at all, and it is its own category rather than part
#: of `admin` for the reason the category exists: "who has taken a copy of the trail" is a
#: question somebody asks on its own, and filing it under `admin` would mix it in with
#: Maintenance Mode and `jvm.config`.
Category = SectionName | Literal["admin", "pipeline", "audit"]

#: What was done. Past tense, because an event records something that already happened --
#: writing one before the change lands would name an actor for something that never did.
#:
#: `attempted` is the exception, and is only ever paired with `refused`: nothing was done, so
#: there is nothing to name. The alternative is inferring an intent from the HTTP method,
#: which would record a refused `POST /catalogs/revert` as a creation -- confidently wrong,
#: where `attempted` is merely incomplete and says so.
Action = Literal[
    "attempted",
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
    "exported",
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

#: Whether this process has indexed the audit collection yet. See `AuditStore._indexed_once`
#: for why the scope is the process and not the store.
_ENSURED = False


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
    token_name: str | None = Field(
        default=None,
        description=(
            "That token's name, as it was when the event was written. Here rather than "
            "looked up from `token_id`, because `GET /tokens` lists only the caller's own: "
            "an Admin reading the trail could otherwise not say which automation of "
            "somebody else's acted. Null on events written before Apchi kept it."
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


class Filters(BaseModel):
    """Which events to read, for the listing and for the export alike.

    One object rather than eight parameters declared in two places. An export is the listing
    without the paging (#215), and two parameter lists that have to stay identical are two
    parameter lists that eventually do not -- the filter added to one and forgotten in the
    other is the whole failure mode, and it is silent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: Category | None = None
    actor: str | None = None
    outcome: Outcome | None = None
    resource: str | None = None
    action: Action | None = None
    apply_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None

    #: The filters that are an equality match on one field of the record, which is exactly
    #: the set that needs its own index. `since` and `until` are a range over `at`, which the
    #: base index already sorts by.
    EQUALITY: ClassVar[tuple[str, ...]] = (
        "category",
        "actor",
        "outcome",
        "resource",
        "action",
        "apply_id",
    )

    def query(self) -> dict[str, Any]:
        """The MongoDB query, with the filters combining as a conjunction.

        There is deliberately no text search over `detail`: it holds why something was
        refused, in prose Apchi writes, and making it searchable would turn those sentences
        into a contract clients match on.
        """
        query: dict[str, Any] = {
            field: value for field in self.EQUALITY if (value := getattr(self, field)) is not None
        }
        window = {k: v for k, v in (("$gte", self.since), ("$lte", self.until)) if v is not None}
        if window:
            query["at"] = window
        return query

    def described(self) -> str:
        """The filters in prose, for the record an export leaves behind.

        Recorded because "who took a copy" is only half the question: a copy of one actor's
        events and a copy of the whole trail are different acts, and the filters are the only
        thing that tells them apart.
        """
        applied = {
            field: getattr(self, field)
            for field in (*self.EQUALITY, "since", "until")
            if getattr(self, field) is not None
        }
        if not applied:
            return "an export of the whole trail"
        named = ", ".join(
            f"{field}={value.isoformat() if isinstance(value, datetime) else value}"
            for field, value in applied.items()
        )
        return f"an export filtered by {named}"


@dataclass(frozen=True)
class Entry:
    """One event's worth of detail, for recording several at once."""

    category: Category
    action: Action
    resource: str | None = None
    snapshot: int | None = None
    outcome: Outcome = "succeeded"
    detail: str | None = None


def _decode(cursor: str) -> tuple[datetime, ObjectId]:
    """The cursor's two halves. Raises `BadCursor` for anything unreadable."""
    at, identifier = decode_cursor(cursor, expected=2)
    try:
        return datetime.fromisoformat(at), ObjectId(identifier)
    except (InvalidId, ValueError) as bad:
        raise BadCursor(cursor) from bad


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
        token_name: str | None = None,
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
            "token_name": token_name,
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

    async def record_each(
        self,
        actor: str,
        via: Via,
        entries: list[Entry],
        *,
        token_id: str | None = None,
        token_name: str | None = None,
        apply_id: str | None = None,
    ) -> list[AuditEvent]:
        """Several events by one actor, in one write.

        One request that edited five resources is one act by one person; five round trips for
        it is five times the traffic for no extra information, and per-operation traffic is
        what this suite's MongoDB falls over on (#69). They share one timestamp, which is
        also more truthful -- they happened in one request.
        """
        if not entries:
            return []
        at = datetime.now(UTC)
        documents = [
            {
                "at": at,
                "actor": actor,
                "via": via,
                "token_id": token_id,
                "token_name": token_name,
                "category": entry.category,
                "resource": entry.resource,
                "action": entry.action,
                "snapshot": entry.snapshot,
                "outcome": entry.outcome,
                "detail": entry.detail,
                "apply_id": apply_id,
            }
            for entry in entries
        ]
        await self._indexed_once()
        result = await self._collection.insert_many(documents)
        # Built from the entries rather than by splatting the documents back: the dicts are
        # typed as their widest value, and a model that forbids unknown fields deserves to be
        # constructed field by field rather than from a mapping mypy cannot check.
        return [
            AuditEvent(
                id=str(identifier),
                at=at,
                actor=actor,
                via=via,
                token_id=token_id,
                token_name=token_name,
                category=entry.category,
                resource=entry.resource,
                action=entry.action,
                snapshot=entry.snapshot,
                outcome=entry.outcome,
                detail=entry.detail,
                apply_id=apply_id,
            )
            for identifier, entry in zip(result.inserted_ids, entries, strict=True)
        ]

    async def page(
        self,
        filters: Filters | None = None,
        *,
        limit: int = 50,
        before: str | None = None,
    ) -> Page[AuditEvent]:
        """History newest first, filtered and paged.

        The keyset predicate is what makes the boundary exact: everything strictly older than
        the cursor's instant, plus anything in that same instant with a lower identifier.
        """
        query = (filters or Filters()).query()
        if before is not None:
            at, identifier = _decode(before)
            query.update(after("at", at, identifier))

        # One more than asked for, so "is there another page" is answered by what came back
        # rather than by a second count query that can disagree with it.
        limit = bounded(limit)
        cursor = self._collection.find(query).sort([("at", DESCENDING), ("_id", DESCENDING)])
        documents = [document async for document in cursor.limit(limit + 1)]
        events = [self._as_event(document) for document in documents[:limit]]
        more = len(documents) > limit
        return Page(
            items=events,
            next=(
                encode_cursor(events[-1].at.isoformat(), events[-1].id) if more and events else None
            ),
        )

    async def stream(self, filters: Filters | None = None) -> AsyncIterator[AuditEvent]:
        """Every matching event, newest first, one at a time.

        The same filters as `page` and no paging at all, which is what an export is. No limit
        either, and that is the point of streaming it: retention is off by default (see the
        module docstring), so this collection is the longest-lived thing in the product and
        its size is unbounded. Assembling the answer before sending it would make Apchi's own
        history the way to exhaust its memory.

        Newest first, like the listing, so the two orders cannot disagree about what "the
        first event" means. The MongoDB cursor stays open while the response is written.
        """
        cursor = self._collection.find((filters or Filters()).query()).sort(
            [("at", DESCENDING), ("_id", DESCENDING)]
        )
        async for document in cursor:
            yield self._as_event(document)

    async def _indexed_once(self) -> None:
        """Once per **process**, before the first write.

        Per process rather than per store, and the distinction is the whole point. One Apchi
        pod is one process with one store, so the two are the same thing in production. Tier 1
        is not: it builds an app per test, so a per-store flag means three `create_index`
        round trips for every test that records anything -- which is now almost all of them,
        and which wedges the test MongoDB exactly as #131 did. Measured twice, both times by
        accident:

            per app start (#138, first attempt)   ~19 tests in five minutes, mongod unresponsive
            per store, once audit was wired here  the same, at the same point in the suite
            per process                           back to normal

        `_ENSURED` is module state, which is a smell worth naming: it exists because the
        number of stores a process creates is an artifact of the test harness rather than
        anything Apchi means. #69 holds the structural fix -- one MongoDB client shared across
        the suite -- and until that lands this is the honest statement of the intent, which
        was always "once, at the start, for this process's database".

        Set only on success, so a MongoDB that was unreachable is retried rather than
        remembered as done. Tests that assert anything about these indexes call
        `ensure_indexes` directly, which always acts.
        """
        global _ENSURED
        if _ENSURED or self._indexed:
            return
        await self.ensure_indexes(self._retention_days)
        _ENSURED = True
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
        """One index per filter the endpoint offers, and nothing else.

        Every query sorts by `(at, _id)` descending, so each filter gets that pair as its
        suffix: MongoDB then satisfies the match and the sort from one index, with no blocking
        sort to grow with the collection.

        Driven off `Filters.EQUALITY` rather than listed again here, so a filter cannot be
        offered without being indexed -- which is the failure this collection punishes, since
        it is the one that outlives everything else.

        Six of them, which would be a lot on a busy collection and is not one here. An event
        is written when somebody changes configuration on a single Trino cluster -- a handful
        of times a day -- so the write amplification six indexes cost is not a consideration,
        and the read side is: with no retention by default this collection outlives everything
        else in the product, and an unindexed filter over it is a scan that gets slower every
        month it is kept.

        `via` deliberately has none. It is on the record so that "jo did this" and "an
        automation jo minted did this" are different answers, but it has three values and is
        asked about alongside an actor rather than instead of one.
        """
        await self._collection.create_index([("at", DESCENDING), ("_id", DESCENDING)])
        for field in Filters.EQUALITY:
            await self._collection.create_index(
                [(field, ASCENDING), ("at", DESCENDING), ("_id", DESCENDING)]
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

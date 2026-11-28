"""Apply as a resource.

An Apply runs Validation, Apply, Verification and Commit -- minutes, not a request.
So it is a resource with an identifier, and its record lives in MongoDB: the event
stream is a view over durable state rather than over in-memory progress.

The record holds the *full* stage history, not just the current stage, so a client
reconnecting mid-Apply replays what it missed instead of seeing a blank timeline.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, computed_field
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError

from app.api.pages import (
    DEFAULT_LIMIT,
    BadCursor,
    Page,
    after,
    bounded,
    decode_cursor,
    encode_cursor,
)
from app.audit.actor import SYSTEM_ACTOR, actor
from app.audit.events import Action, AuditStore, Category, Outcome
from app.auth.identity import Identity, Via
from app.logging import apply_id_var
from app.pipeline.auto_rollback import INCIDENT_MESSAGE
from app.pipeline.validation import ValidationFailed
from app.pipeline.verification import CheckResult
from app.sections.base import ValidationFailure

logger = logging.getLogger(__name__)


class Stage(StrEnum):
    VALIDATING = "validating"
    APPLYING = "applying"
    #: Restarting Trino so it adopts what it cannot adopt while running. Only present on
    #: an Apply that needed one.
    ROLLING_OUT = "rolling_out"
    VERIFYING = "verifying"
    COMMITTING = "committing"
    #: Auto Rollback, on its way to one of the two failed terminals.
    ROLLING_BACK = "rolling_back"
    SUCCEEDED = "succeeded"
    #: The Apply did not go through and the Cluster is back on its latest Snapshot.
    FAILED = "failed"
    #: The Apply did not go through and Auto Rollback could not put the Cluster back.
    #: Apchi has stopped touching it and Maintenance Mode is engaged.
    INCIDENT = "incident"


#: An Apply in one of these is over. Anything else means it is in flight, and the
#: Candidate is frozen. An incident is over in this sense too: nothing further will
#: happen to it, and what keeps Operators off the Cluster is Maintenance Mode, not a
#: freeze that would also block the Admin action that clears it.
TERMINAL: frozenset[Stage] = frozenset({Stage.SUCCEEDED, Stage.FAILED, Stage.INCIDENT})

#: The order the pipeline walks. Stages stay separate internally even where the UI
#: presents them as one action.
PIPELINE: tuple[Stage, ...] = (
    Stage.VALIDATING,
    Stage.APPLYING,
    Stage.ROLLING_OUT,
    Stage.VERIFYING,
    Stage.COMMITTING,
)

#: Failing in one of these means the Cluster was touched, so Auto Rollback runs.
#:
#: A Rollout failure belongs here for the same reason Apply does: the configuration
#: reached the Cluster and the Cluster has not come back on it.
#:
#: Validation is excluded because nothing reached the Cluster -- there is nothing to
#: undo, and the Candidate is left for the Operator to fix. Commit is excluded for the
#: opposite reason: the configuration was applied *and verified*, and what failed was
#: MongoDB. Rolling back there would tear down a healthy Cluster to recover from a
#: database error.
ROLLED_BACK_FROM: frozenset[Stage] = frozenset({Stage.APPLYING, Stage.ROLLING_OUT, Stage.VERIFYING})


class Rollback(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class StageEvent(BaseModel):
    stage: Stage
    at: datetime
    detail: str | None = None


#: An Operator's Apply promotes the Candidate and commits a Snapshot; an Admin Apply
#: delivers today's Admin values over the latest Snapshot and commits none (§14).
ApplyKind = Literal["apply", "admin_apply"]


class ApplyRecord(BaseModel):
    id: str
    kind: ApplyKind = Field(
        default="apply",
        description=(
            "Which of the two pipelines ran. Both walk the same stages, so without this an "
            "Admin Apply reads as an Operator's Apply that never committed a Snapshot -- the "
            "same record a failure leaves. Applies recorded before this field existed read "
            "as `apply`."
        ),
    )
    started_by: str | None = Field(
        default=None,
        description=(
            "The subject of whoever started it. Null only on Applies recorded before Apchi "
            "kept this; the audit's `attempted` event for the same `apply_id` says it too."
        ),
    )
    started_via: Via | None = Field(
        default=None,
        description=(
            "How they authenticated, because an Apply a pipeline started with a token and "
            "one a person clicked are different answers to *who did this*."
        ),
    )
    stage: Stage
    history: list[StageEvent] = Field(default_factory=list)
    failure_reason: str | None = None
    interrupted: bool = Field(
        default=False,
        description=(
            "True when an Apchi restart ended this Apply rather than a failure in it. "
            "The Cluster was still put back; what differs is why."
        ),
    )
    rollback: Rollback | None = Field(
        default=None,
        description=(
            "The outcome of Auto Rollback. Absent when none was attempted, which "
            "means nothing reached the Cluster."
        ),
    )
    operator_message: str | None = Field(
        default=None,
        description="What to show an Operator. Set when an Apply ends in an incident.",
    )
    warnings: list[ValidationFailure] = Field(
        default_factory=list,
        description=(
            "What Validation found worth saying and not worth refusing over. An Apply that "
            "succeeded with warnings succeeded: these are reported so an Operator can see "
            "them next to the Apply that carried them, rather than having to run a separate "
            "Validation to find out. A Catalog the probe could not connect to is the one "
            "case today (§6)."
        ),
    )
    failures: list[ValidationFailure] = Field(
        default_factory=list,
        description=(
            "Every reason Validation refused this Apply, when that is how it failed -- the "
            "same list a Validation's own record carries. `failure_reason` holds them too, "
            "joined into one sentence; this is what says which Section and resource each "
            "belongs to. Empty for any other outcome."
        ),
    )
    verification: list[CheckResult] = Field(
        default_factory=list,
        description=(
            "How each of Verification's checks came out, in the order they run. Empty on an "
            "Apply that never reached Verification -- a Validation failure means nothing was "
            "delivered and nothing was checked.\n\n"
            "Verification **stops at the first failure**, so at most one check is `failed` "
            "and everything after it is `not_reached` rather than passed or failed. That is "
            "not a gap in the record: there is no point asking a broken Cluster the next "
            "question, and Auto Rollback is waiting on the answer.\n\n"
            "This Apply's own Verification only. Auto Rollback verifies again against the "
            "configuration it restored, which is a different question and is reported by "
            "`rollback`."
        ),
    )
    base_snapshot: int | None = None
    snapshot: int | None = Field(
        default=None, description="The Snapshot this Apply committed, when it succeeded."
    )
    started_at: datetime
    finished_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rolled_out(self) -> bool:
        """Whether this Apply restarted Trino to make its changes live.

        Derived from the history rather than recorded beside it, so the two cannot
        disagree. It is about the Apply's own Rollout: an Apply that failed before
        reaching one may still have been restarted by its Auto Rollback, which the
        rollback outcome is what reports.
        """
        return any(event.stage is Stage.ROLLING_OUT for event in self.history)

    @property
    def in_flight(self) -> bool:
        return self.stage not in TERMINAL


class ApplyInFlight(Exception):
    """Another Apply holds the Candidate frozen."""

    def __init__(self, running: ApplyRecord) -> None:
        self.running = running
        super().__init__(running.id)


class ApplyStore:
    def __init__(self, database: AsyncDatabase[dict[str, Any]]) -> None:
        self._collection = database["applies"]
        # Finding nothing in flight and recording the new Apply are one step: with an await
        # between them, requests that arrived together each found nothing, and as many
        # pipelines as requests ran against one Cluster. A lock in the process, because one
        # Apchi is the supported shape -- a second replica is a second pipeline anyway.
        self._starting = asyncio.Lock()

    @staticmethod
    def _as_record(document: dict[str, Any]) -> ApplyRecord:
        document.pop("_id", None)
        return ApplyRecord.model_validate(document)

    async def create(self, base_snapshot: int | None, kind: ApplyKind = "apply") -> ApplyRecord:
        """Record a new Apply, or raise `ApplyInFlight` naming the one already running."""
        async with self._starting:
            if (running := await self.in_flight()) is not None:
                raise ApplyInFlight(running)
            return await self._insert(base_snapshot, kind)

    async def _insert(self, base_snapshot: int | None, kind: ApplyKind) -> ApplyRecord:
        now = datetime.now(UTC)
        # Read on the request that asked for the Apply, where the caller is known -- the
        # same reason `ApplyRunner.start` reads it there rather than inside its task.
        who = actor()
        record = ApplyRecord(
            id=f"apl_{uuid.uuid4().hex[:16]}",
            kind=kind,
            started_by=who.subject,
            started_via=who.via,
            stage=Stage.VALIDATING,
            history=[StageEvent(stage=Stage.VALIDATING, at=now)],
            base_snapshot=base_snapshot,
            started_at=now,
        )
        await self._collection.insert_one({"_id": record.id, **record.model_dump(mode="json")})
        return record

    async def get(self, apply_id: str) -> ApplyRecord | None:
        document = await self._collection.find_one({"_id": apply_id})
        return None if document is None else self._as_record(document)

    async def page(
        self, limit: int = DEFAULT_LIMIT, before: str | None = None
    ) -> Page[ApplyRecord]:
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

    async def in_flight(self) -> ApplyRecord | None:
        """The Apply currently freezing the Candidate, if any."""
        document = await self._collection.find_one({"stage": {"$nin": list(TERMINAL)}})
        return None if document is None else self._as_record(document)

    async def advance(self, apply_id: str, stage: Stage, detail: str | None = None) -> None:
        event = StageEvent(stage=stage, at=datetime.now(UTC), detail=detail)
        update: dict[str, Any] = {
            "$set": {"stage": stage.value},
            "$push": {"history": event.model_dump(mode="json")},
        }
        if stage in TERMINAL:
            update["$set"]["finished_at"] = event.at.isoformat()
        await self._collection.update_one({"_id": apply_id}, update)

    async def record_snapshot(self, apply_id: str, number: int) -> None:
        await self._collection.update_one({"_id": apply_id}, {"$set": {"snapshot": number}})

    async def record_warnings(self, apply_id: str, warnings: Sequence[ValidationFailure]) -> None:
        """What Validation found worth saying. Written even when empty, which is almost
        always: a field that is sometimes absent is one a client has to special-case."""
        await self._collection.update_one(
            {"_id": apply_id},
            {"$set": {"warnings": [warning.model_dump(mode="json") for warning in warnings]}},
        )

    async def record_failures(self, apply_id: str, failures: Sequence[ValidationFailure]) -> None:
        await self._collection.update_one(
            {"_id": apply_id},
            {"$set": {"failures": [failure.model_dump(mode="json") for failure in failures]}},
        )

    async def record_verification(self, apply_id: str, checks: Sequence[CheckResult]) -> None:
        """Which checks ran and how they came out. Written on both paths, because the
        failing one is where this is read."""
        await self._collection.update_one(
            {"_id": apply_id},
            {"$set": {"verification": [check.model_dump(mode="json") for check in checks]}},
        )

    async def fail(self, apply_id: str, reason: str) -> None:
        await self._collection.update_one({"_id": apply_id}, {"$set": {"failure_reason": reason}})
        await self.advance(apply_id, Stage.FAILED, detail=reason)

    async def mark_interrupted(self, apply_id: str) -> None:
        await self._collection.update_one({"_id": apply_id}, {"$set": {"interrupted": True}})

    async def record_rollback(self, apply_id: str, outcome: Rollback) -> None:
        await self._collection.update_one({"_id": apply_id}, {"$set": {"rollback": outcome.value}})

    async def declare_incident(self, apply_id: str, reason: str, message: str) -> None:
        """The other failed terminal. The failure reason stays the diagnostic one --
        an Operator has to be able to diagnose without log access -- and the message
        is what the UI shows."""
        await self._collection.update_one(
            {"_id": apply_id},
            {"$set": {"failure_reason": reason, "operator_message": message}},
        )
        await self.advance(apply_id, Stage.INCIDENT, detail=message)


@runtime_checkable
class ApplyEngine(Protocol):
    """What the pipeline does at each stage."""

    async def validate(self) -> None: ...
    async def apply(self) -> None: ...
    async def verify(self) -> None: ...
    async def commit(self) -> int | None: ...

    def rollout_needed(self) -> bool:
        """Whether the Candidate's changes require Trino restarted. Known once Validation
        has planned, which is before the stage that asks."""
        ...

    def warnings(self) -> list[ValidationFailure]:
        """What Validation found worth saying and not worth refusing over. Known once
        Validation has run; empty before that, and empty most of the time after."""
        ...

    def verification(self) -> list[CheckResult]:
        """How each of Verification's checks came out. Empty until Verification has run,
        and kept when it fails -- which is when anybody reads it."""
        ...

    async def roll_out(self) -> None:
        """Restart Trino and wait for it to come back."""
        ...

    async def roll_back(self) -> None:
        """Put the Cluster back on its latest Snapshot. Raises if it cannot."""
        ...

    async def declare_incident(self, reason: str) -> None:
        """Stop touching the Cluster: engage Maintenance Mode and alert."""
        ...


class NoOpEngine:
    """Does nothing. Used where the stages matter but the work does not."""

    async def validate(self) -> None: ...
    async def apply(self) -> None: ...
    async def verify(self) -> None: ...
    async def commit(self) -> int | None:
        return None

    def rollout_needed(self) -> bool:
        return False

    def warnings(self) -> list[ValidationFailure]:
        return []

    def verification(self) -> list[CheckResult]:
        return []

    async def roll_out(self) -> None: ...
    async def roll_back(self) -> None: ...
    async def declare_incident(self, reason: str) -> None: ...


#: Builds the engine for one Apply. A factory rather than a single instance because
#: an engine carries the state its stages share.
EngineFactory = Callable[[str], ApplyEngine]


class ApplyRunner:
    """Drives an Apply through its stages, in the background."""

    def __init__(
        self,
        store: ApplyStore,
        engine_factory: EngineFactory,
        audit: AuditStore,
        category: Category = "pipeline",
    ) -> None:
        self._store = store
        self._engine_factory = engine_factory
        self._audit = audit
        # `admin` for the runner driving an Admin Apply. §20 is explicit that an Admin change
        # is not a Section, and §14 gives it no Snapshot -- so the category is what tells the
        # two kinds of Apply apart in the trail, since both end in the same stages.
        self._category = category
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, record: ApplyRecord) -> None:
        # The actor is read **here**, not inside the task. `start` runs on the request that
        # asked for the Apply, where the contextvar is set; the task it creates outlives that
        # request, and a `contextvar` read after it has ended would resolve to the system
        # actor. Of every record in §20's trail, "who promoted this configuration" is the one
        # that least deserves to say "apchi" (#140).
        who = actor()
        task = asyncio.create_task(self._run(record.id, who))
        # Hold a reference: a bare create_task may be garbage collected mid-flight.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _audited(
        self,
        who: Identity,
        apply_id: str,
        action: Action,
        *,
        snapshot: int | None = None,
        outcome: Outcome = "succeeded",
        detail: str | None = None,
    ) -> None:
        """One pipeline event. Never fatal.

        An Apply that reached the Cluster must not be reported as failed because its audit
        row would not write -- the Cluster has already changed, and a failure here would
        send the caller down the incident path for a bookkeeping error. The opposite rule
        holds for a Candidate edit, where nothing has happened yet and `CandidateStore.save`
        lets it propagate.
        """
        try:
            await self._audit.record(
                who.subject,
                who.via,
                self._category,
                action,
                snapshot=snapshot,
                outcome=outcome,
                detail=detail,
                token_id=who.token_id,
                token_name=who.token_name,
                apply_id=apply_id,
            )
        except PyMongoError:
            logger.exception("could not record an apply in the audit history")

    async def _run(self, apply_id: str, who: Identity) -> None:
        # Every record emitted from here carries the identifier, so one id yields
        # the whole story of a failure.
        token = apply_id_var.set(apply_id)
        engine = self._engine_factory(apply_id)
        steps: tuple[tuple[Stage, Callable[[], Awaitable[Any]]], ...] = (
            (Stage.VALIDATING, engine.validate),
            (Stage.APPLYING, engine.apply),
            (Stage.ROLLING_OUT, engine.roll_out),
            (Stage.VERIFYING, engine.verify),
            (Stage.COMMITTING, engine.commit),
        )
        reached = Stage.VALIDATING
        try:
            snapshot: int | None = None
            for index, (stage, step) in enumerate(steps):
                # A Candidate that changed nothing Trino adopts by restarting gets no
                # Rollout, and no stage for one: half the Sections reach the Cluster
                # without a restart, and an Apply that did not restart anything should not
                # look like one that did.
                if stage is Stage.ROLLING_OUT and not engine.rollout_needed():
                    continue
                reached = stage
                if index:  # the first stage is recorded at creation
                    await self._store.advance(apply_id, stage)
                logger.info("apply stage", extra={"stage": stage.value})
                result = await step()
                if stage is Stage.VALIDATING:
                    # Recorded here rather than at the end, so an Apply that fails a later
                    # stage still carries what Validation found.
                    await self._store.record_warnings(apply_id, engine.warnings())
                if stage is Stage.VERIFYING:
                    await self._store.record_verification(apply_id, engine.verification())
                if stage is Stage.COMMITTING and isinstance(result, int):
                    snapshot = result
            if snapshot is not None:
                await self._store.record_snapshot(apply_id, snapshot)
            await self._store.advance(apply_id, Stage.SUCCEEDED)
            logger.info("apply succeeded", extra={"snapshot": snapshot})
            # The record that ties an actor to a Snapshot, and the only one that can: a
            # Snapshot knows which Apply produced it, and this is what says who asked.
            await self._audited(who, apply_id, "applied", snapshot=snapshot)
        except Exception as exc:
            logger.exception("apply failed")
            # Before anything else in this handler: a failed Verification is the most likely
            # reason to be here, and which check failed is what the record has to carry once
            # Auto Rollback has moved the Cluster on.
            await self._store.record_verification(apply_id, engine.verification())
            if isinstance(exc, ValidationFailed):
                await self._store.record_failures(apply_id, exc.failures)
            reason = f"{type(exc).__name__}: {exc}"
            # The stage is the detail worth keeping. §10 sends a partially applied Apply to an
            # incident state rather than improvising compensating statements, and a human
            # arriving there wants to know how far it got before anything else.
            await self._audited(
                who,
                apply_id,
                "applied",
                outcome="failed",
                detail=f"failed in {reached.value}: {reason}",
            )
            try:
                if reached in ROLLED_BACK_FROM:
                    await self._roll_back(apply_id, engine, reason, who)
                else:
                    await self._store.fail(apply_id, reason)
            except PyMongoError:
                # MongoDB stopped answering, so this Apply can be neither finished nor recorded
                # as failed, and it holds the Candidate frozen. An outage long enough for the
                # liveness probe restarts Apchi and startup recovery resolves it; a shorter one
                # left it in flight until somebody restarted the pod.
                logger.exception(
                    "could not record how this apply ended; resolving it once MongoDB answers"
                )
                self._resolve_when_reachable(apply_id)
        finally:
            apply_id_var.reset(token)

    def _resolve_when_reachable(self, apply_id: str) -> None:
        """Resolve the Apply as startup recovery would, as soon as MongoDB answers."""

        async def resolve() -> None:
            while True:
                try:
                    await recover_interrupted(
                        self._store,
                        self._engine_factory,
                        self._audit,
                        # This runner's own kind, which is the kind of the Apply it ran.
                        admin_engine_factory=self._engine_factory,
                        reason=UNRECORDED_REASON,
                    )
                except PyMongoError:
                    await asyncio.sleep(STORE_RETRY_SECONDS)
                else:
                    logger.warning(
                        "resolved an apply MongoDB had stopped recording",
                        extra={"resolved_apply": apply_id},
                    )
                    return

        task = asyncio.create_task(resolve())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _roll_back(
        self, apply_id: str, engine: ApplyEngine, reason: str, who: Identity
    ) -> None:
        """One attempt, and no retry whatever it does.

        A failure to roll back is not a failure of this Apply to report and move on
        from: the Cluster is in a state nobody has described, so the outcome is an
        incident and Apchi stops touching it.
        """
        await self._store.advance(apply_id, Stage.ROLLING_BACK, detail=reason)
        logger.warning("rolling back to the latest snapshot", extra={"why": reason})
        try:
            await engine.roll_back()
        except Exception as rollback_exc:
            logger.exception("auto rollback failed")
            await self._store.record_rollback(apply_id, Rollback.FAILED)
            combined = f"{reason}. Auto Rollback then failed: {rollback_exc}"
            await engine.declare_incident(combined)
            await self._store.declare_incident(apply_id, combined, INCIDENT_MESSAGE)
            # Attributed to Apchi, not to whoever started the Apply: they asked for a
            # configuration, not for a rollback, and §14 has just engaged Maintenance Mode on
            # a Cluster in a state nobody has described. The trail has to show that as
            # Apchi's action and the Apply as theirs.
            await self._audited(
                SYSTEM_ACTOR,
                apply_id,
                "rolled_back",
                outcome="failed",
                detail=combined,
            )
            return
        await self._store.record_rollback(apply_id, Rollback.SUCCEEDED)
        await self._store.fail(apply_id, reason)
        logger.info("cluster returned to its latest snapshot")
        # No resulting Snapshot: an Auto Rollback returns the Cluster to the one it was
        # already on, and recording a number here would read as a new one having been
        # committed.
        await self._audited(SYSTEM_ACTOR, apply_id, "rolled_back", detail=reason)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()


INTERRUPTED_REASON = "Apchi restarted while this Apply was in flight; it was not completed."
UNRECORDED_REASON = (
    "MongoDB stopped answering while this Apply was in flight; it was not completed."
)

#: How often a runner retries resolving an Apply it could not record, while MongoDB is down.
STORE_RETRY_SECONDS = 5.0


async def recover_interrupted(
    store: ApplyStore,
    engine_factory: EngineFactory,
    audit: AuditStore | None = None,
    admin_engine_factory: EngineFactory | None = None,
    reason: str = INTERRUPTED_REASON,
) -> list[str]:
    """Resolve Applies left in flight by a restart, or by a MongoDB that stopped answering.

    The Candidate is frozen for the whole of an Apply, so an Apchi crash mid-flight
    would freeze it permanently. Whatever else recovery decides, the Candidate must
    end up unfrozen -- that is the part that must never be left to chance, which is why
    the record is resolved before the Cluster is touched.

    An interrupted Apply may also have left the Cluster diverged: Apply writes the
    catalog Secret before issuing DDL, so a crash in that window leaves the durable copy
    naming a catalog Trino never got. Recovery therefore rolls the Cluster back exactly
    as a failed Apply does -- one attempt, no retry, no Snapshot -- and escalates to an
    incident if that attempt fails. It can do this because the rollback plan is a diff of
    two durable records and needs nothing from the process that died.
    """
    recovered: list[str] = []
    while (record := await store.in_flight()) is not None:
        reached = record.stage
        await store.mark_interrupted(record.id)
        await store.fail(record.id, reason)
        logger.warning(
            "recovered interrupted apply",
            extra={"recovered_apply": record.id, "reached": reached.value},
        )
        recovered.append(record.id)
        # Audited with the system actor: this resolution changes the Cluster and nobody
        # asked for it. The actor who started the Apply is not responsible for what a
        # restart did to it, and the stage it reached is what a human needs here.
        if audit is not None:
            try:
                await audit.record(
                    SYSTEM_ACTOR.subject,
                    SYSTEM_ACTOR.via,
                    "pipeline",
                    "applied",
                    outcome="failed",
                    detail=f"interrupted in {reached.value}: {reason}",
                    apply_id=record.id,
                )
            except PyMongoError:
                logger.exception("could not record a recovered apply in the audit history")

        if reached in ROLLED_BACK_FROM:
            # The rollback an Apply of this kind would have made. An Operator's keeps the
            # Admin values -- they survive an Operator's rollback (invariant 2) -- but an
            # interrupted *Admin* Apply is one whose Admin values are what may have broken the
            # Cluster, so they go back to what the Cluster last had, as they would have had
            # the process not died. Recovering one with the Operator's engine left them in place.
            factory = (
                admin_engine_factory
                if record.kind == "admin_apply" and admin_engine_factory is not None
                else engine_factory
            )
            await _restore_after_interruption(store, factory, record.id, reason)
    return recovered


async def _restore_after_interruption(
    store: ApplyStore, engine_factory: EngineFactory, apply_id: str, reason: str
) -> None:
    """The same bounded attempt Auto Rollback makes, from a process that did not run
    the Apply."""
    token = apply_id_var.set(apply_id)
    try:
        engine = engine_factory(apply_id)
        try:
            await engine.roll_back()
        except Exception as exc:
            logger.exception("rollback of an interrupted apply failed")
            await store.record_rollback(apply_id, Rollback.FAILED)
            failed = f"{reason} Auto Rollback then failed: {exc}"
            await engine.declare_incident(failed)
            await store.declare_incident(apply_id, failed, INCIDENT_MESSAGE)
            return
        await store.record_rollback(apply_id, Rollback.SUCCEEDED)
        logger.info("cluster returned to its latest snapshot after a restart")
    finally:
        apply_id_var.reset(token)


def _started_in(cursor: str) -> tuple[datetime, str]:
    """The instant and the id a cursor names."""
    at, identifier = decode_cursor(cursor, expected=2)
    try:
        return datetime.fromisoformat(at), identifier
    except ValueError as bad:
        raise BadCursor(cursor) from bad

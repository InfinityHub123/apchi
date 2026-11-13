"""What Apchi knows about the Cluster, and what only the coordinator can say.

Two routes because they cost different things, and §21 asks for the state to be *always*
visible -- which means something polls it. `GET /cluster` reads MongoDB and nothing else, so
a console can poll it without turning every viewer into traffic against Trino. The probe is
a separate call somebody asks for.

Health is reported in parts rather than as one verdict. A Verification is the only thing in
this product that decides a Cluster is healthy, and it runs during an Apply (§8) -- so the
last one is history, and presenting it as a current reading would tell an Operator the
Cluster is fine because it was fine when somebody last applied. `GET /health` is not that
reading either: it is Apchi's own liveness and the chart's probe, which is why it reports
nothing about Trino.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ApplyStoreDep, ClusterDep, MaintenanceStoreDep, SnapshotStoreDep
from app.pipeline.applies import ApplyRecord, Stage
from app.pipeline.maintenance import MaintenanceState

router = APIRouter(prefix="/cluster", tags=["cluster"])


class LastApply(BaseModel):
    """The most recent Apply, as far as the Cluster's state is concerned."""

    model_config = ConfigDict(extra="forbid")

    id: str
    stage: Stage
    started_at: datetime
    finished_at: datetime | None = Field(
        default=None, description="Null while it is still in flight."
    )
    verified: bool | None = Field(
        description=(
            "Whether Verification passed. Null when this Apply never reached Verification, "
            "which is a different answer from `false`: a Validation failure means nothing "
            "was delivered and nothing was checked."
        )
    )
    snapshot: int | None = Field(
        default=None, description="The Snapshot it committed, when it succeeded."
    )


class ClusterState(BaseModel):
    """What Apchi can answer without asking Trino anything."""

    model_config = ConfigDict(extra="forbid")

    snapshot: int | None = Field(
        description="The committed Snapshot the Cluster is running; null before the first."
    )
    snapshot_at: datetime | None = Field(
        default=None, description="When that Snapshot was committed."
    )
    maintenance: MaintenanceState = Field(
        description=(
            "Whether Operator changes are disabled, why, and whether an Admin engaged it or "
            "Apchi did after a failed Auto Rollback. Readable here by any caller, because "
            "the people it stops are the ones who need to know -- `/admin/maintenance-mode` "
            "is where an Admin changes it, and that stays Admin-only."
        )
    )
    last_apply: LastApply | None = Field(
        default=None,
        description=(
            "The most recent Apply, or null on a Cluster nothing has been applied to. Its "
            "Verification is the last word on whether the Cluster was healthy, and it is "
            "**history**: it says nothing about now. Probe for that."
        ),
    )


class Probe(BaseModel):
    """One reading, taken when asked."""

    model_config = ConfigDict(extra="forbid")

    at: datetime = Field(
        description="When this reading was taken. Already stale by the time it is read."
    )
    coordinator: Literal["up", "starting", "unreachable"] = Field(
        description=(
            "`starting` is its own answer rather than a kind of down: a coordinator that is "
            "coming back from a Rollout refuses queries and is not broken."
        )
    )
    workers: int | None = Field(
        default=None,
        description=(
            "Active workers, and only when asked for. Null otherwise, and null when the "
            "coordinator could not be reached or the count failed -- never 0 for 'unknown', "
            "because a Cluster with no workers is a real and different state."
        ),
    )


def _verified(record: ApplyRecord) -> bool | None:
    """Whether this Apply's Verification passed, from the stages it actually walked.

    Derived rather than stored: the pipeline records stage transitions (§20), and an Apply
    that reached Verification and then went on to Commit is the only shape that means it
    passed. Anything after Verification that is not Commit is Auto Rollback or an incident.
    """
    reached = [event.stage for event in record.history]
    if Stage.VERIFYING not in reached:
        return None
    return Stage.COMMITTING in reached or record.stage is Stage.SUCCEEDED


@router.get("", response_model=ClusterState, summary="What Apchi knows about the Cluster")
async def state(
    snapshots: SnapshotStoreDep,
    maintenance: MaintenanceStoreDep,
    applies: ApplyStoreDep,
) -> ClusterState:
    """Three reads against MongoDB and nothing outbound, so this is the one to poll.

    It answers §21's always-visible pair -- the committed Snapshot and what is known about
    the Cluster's health -- plus the state that decides whether an Operator can edit at all.
    """
    latest = await snapshots.latest()
    page = await applies.page(limit=1)
    recent = page.items[0] if page.items else None

    return ClusterState(
        snapshot=None if latest is None else latest.number,
        snapshot_at=None if latest is None else latest.created_at,
        maintenance=await maintenance.get(),
        last_apply=(
            None
            if recent is None
            else LastApply(
                id=recent.id,
                stage=recent.stage,
                started_at=recent.started_at,
                finished_at=recent.finished_at,
                verified=_verified(recent),
                snapshot=recent.snapshot,
            )
        ),
    )


@router.get("/probe", response_model=Probe, summary="Ask the coordinator how it is, now")
async def probe(
    cluster: ClusterDep,
    workers: Annotated[
        bool,
        Query(
            description=(
                "Also count active workers. Off by default because it is a SQL query "
                "against `system.runtime.nodes` where the reachability check is one HTTP "
                "request, so a caller that only wants to know whether Trino is answering "
                "does not pay for a query."
            )
        ),
    ] = False,
) -> Probe:
    """A live reading, and the only thing here that leaves Apchi.

    Deliberately not folded into `GET /cluster`: that one is polled, and a console with a
    health indicator open on twenty desks would otherwise be twenty requests a second
    arriving at a coordinator whose job is queries.

    It never fails. An unreachable coordinator is the answer to this question, not an error
    serving it -- a 503 here would make "Trino is down" indistinguishable from "Apchi is
    broken", which is the distinction an Operator opens this for.
    """
    starting = await cluster.trino.is_starting()
    coordinator: Literal["up", "starting", "unreachable"] = (
        "unreachable" if starting is None else "starting" if starting else "up"
    )

    count: int | None = None
    if workers and coordinator == "up":
        try:
            count = await cluster.trino.active_worker_count()
        except Exception:
            # Same reason the reachability check does not raise: a coordinator that answers
            # /v1/info and refuses a query is a reading, not a failure of this endpoint.
            count = None

    return Probe(at=datetime.now(UTC), coordinator=coordinator, workers=count)

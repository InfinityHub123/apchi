"""Shared request dependencies."""

from typing import Annotated

from fastapi import Depends, Request

from app.api.errors import Conflict, MaintenanceModeEngaged
from app.pipeline.applies import ApplyStore
from app.pipeline.candidate import CandidateStore
from app.pipeline.maintenance import MaintenanceStore
from app.pipeline.snapshots import SnapshotStore
from app.pipeline.validations import ValidationStore


def candidate_store(request: Request) -> CandidateStore:
    store: CandidateStore = request.app.state.candidate_store
    return store


CandidateStoreDep = Annotated[CandidateStore, Depends(candidate_store)]


def snapshot_store(request: Request) -> SnapshotStore:
    store: SnapshotStore = request.app.state.snapshot_store
    return store


SnapshotStoreDep = Annotated[SnapshotStore, Depends(snapshot_store)]


def apply_store(request: Request) -> ApplyStore:
    store: ApplyStore = request.app.state.apply_store
    return store


ApplyStoreDep = Annotated[ApplyStore, Depends(apply_store)]


def validation_store(request: Request) -> ValidationStore:
    store: ValidationStore = request.app.state.validation_store
    return store


ValidationStoreDep = Annotated[ValidationStore, Depends(validation_store)]


def maintenance_store(request: Request) -> MaintenanceStore:
    store: MaintenanceStore = request.app.state.maintenance_store
    return store


MaintenanceStoreDep = Annotated[MaintenanceStore, Depends(maintenance_store)]


async def mutations_enabled(maintenance: MaintenanceStoreDep) -> None:
    """Rejects a mutation while Maintenance Mode is engaged.

    Either an Admin disabled changes for a platform upgrade, or Apchi did because the
    Cluster's state is unknown after a failed Auto Rollback. Reads are unaffected, and
    so are Admin operations -- releasing the mode has to work while it is engaged.
    """
    state = await maintenance.get()
    if state.engaged:
        raise MaintenanceModeEngaged(
            "Changes are temporarily disabled. "
            + (state.reason or "An Admin has engaged Maintenance Mode.")
        )


MutationsEnabled = Depends(mutations_enabled)


async def operator_mutation_allowed(
    applies: ApplyStoreDep, maintenance: MaintenanceStoreDep
) -> None:
    """The one gate every Operator edit passes.

    Two reasons an edit is refused, and they are not the same thing. Maintenance Mode
    is the broader and is checked first. The freeze is narrower and temporary: the
    Candidate is frozen for the whole of an Apply so that what is committed is what was
    verified. Both are one dependency rather than two, so a new mutating route cannot
    pick up half the gate.
    """
    await mutations_enabled(maintenance)

    running = await applies.in_flight()
    if running is not None:
        raise Conflict(
            f"The Configuration Candidate is frozen: Apply {running.id} is in flight "
            f"({running.stage.value}). Changes are rejected until it finishes."
        )


OperatorMutationAllowed = Depends(operator_mutation_allowed)

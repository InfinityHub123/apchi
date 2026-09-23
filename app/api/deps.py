"""Shared request dependencies."""

from typing import Annotated

from fastapi import Depends, Request

from app.api.errors import Conflict
from app.pipeline.applies import ApplyStore
from app.pipeline.candidate import CandidateStore
from app.pipeline.snapshots import SnapshotStore


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


async def candidate_not_frozen(applies: ApplyStoreDep) -> None:
    """Rejects a mutation while an Apply is in flight.

    The Candidate is frozen for the whole of an Apply -- every engine, not only the
    one that restarts Trino -- so that what is committed is what was verified.
    """
    running = await applies.in_flight()
    if running is not None:
        raise Conflict(
            f"The Configuration Candidate is frozen: Apply {running.id} is in flight "
            f"({running.stage.value}). Changes are rejected until it finishes."
        )


CandidateUnfrozen = Depends(candidate_not_frozen)

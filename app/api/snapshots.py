"""Snapshots: the history an Operator can return to."""

from fastapi import APIRouter

from app.api.deps import SnapshotStoreDep
from app.api.errors import NotFound
from app.pipeline.snapshots import Snapshot

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


@router.get("", response_model=list[Snapshot], summary="Committed Snapshots, newest first")
async def list_snapshots(snapshots: SnapshotStoreDep) -> list[Snapshot]:
    return await snapshots.list()


@router.get("/{number}", response_model=Snapshot, summary="One Snapshot's configuration")
async def get_snapshot(number: int, snapshots: SnapshotStoreDep) -> Snapshot:
    snapshot = await snapshots.get(number)
    if snapshot is None:
        raise NotFound(f"No Snapshot numbered {number}.")
    return snapshot

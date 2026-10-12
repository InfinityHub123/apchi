"""Snapshots: the history an Operator can return to."""

from fastapi import APIRouter

from app.api.deps import SnapshotStoreDep
from app.api.errors import NotFound
from app.pipeline.configuration import Configuration, of_snapshot
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


@router.get(
    "/{number}/export",
    response_model=Configuration,
    summary="One Snapshot as a portable configuration",
)
async def export_snapshot(number: int, snapshots: SnapshotStoreDep) -> Configuration:
    """For taking a configuration somewhere else, or bringing one here to edit.

    **The response holds real credentials.** Snapshots store actual secret values rather
    than references (§12), so every connection password in this configuration is in the
    body in the clear. That is what makes an import work rather than need re-entering.

    The same content as `GET /snapshots/{number}`, framed as a document: a `format` a reader
    can refuse on, and provenance a human can judge. Nothing here gates an import.
    """
    snapshot = await snapshots.get(number)
    if snapshot is None:
        raise NotFound(f"No Snapshot numbered {number}.")
    return of_snapshot(snapshot.number, snapshot.sections)

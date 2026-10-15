"""Snapshots: the history an Operator can return to."""

from fastapi import APIRouter

from app.api.deps import SnapshotStoreDep
from app.api.errors import NotFound, bad_cursor
from app.api.pages import DEFAULT_LIMIT, BadCursor, BeforeParam, LimitParam, Page
from app.pipeline.configuration import Configuration, of_snapshot
from app.pipeline.snapshots import Snapshot

router = APIRouter(prefix="/snapshots", tags=["snapshots"])


@router.get("", response_model=Page[Snapshot], summary="Committed Snapshots, newest first")
async def list_snapshots(
    snapshots: SnapshotStoreDep,
    limit: LimitParam = DEFAULT_LIMIT,
    before: BeforeParam = None,
) -> Page[Snapshot]:
    """Newest first, by Snapshot number."""
    try:
        return await snapshots.page(limit=limit, before=before)
    except BadCursor as bad:
        raise bad_cursor() from bad


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

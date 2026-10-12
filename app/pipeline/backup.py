"""Apchi's state as one document: what MongoDB holds and the Cluster cannot say again.

#116 settled what the §18 disaster actually costs. Losing MongoDB does not lose the
configuration -- re-adoption reads Apchi's own output back off the coordinator and rebuilds
every Section -- so this export is not the road back to a working Cluster. It is the road
back to everything else:

- the numbered Snapshots, which are what §11's Section Revert and Full Rollback aim at, and
  which nothing in Kubernetes holds;
- the Admin values, applied through the pipeline and deliberately never recorded in a
  Snapshot (invariant 9, §14), so nothing else records them at all;
- the Candidate's staged edits, the one thing no recovery can reach, because an edit that was
  never applied was never on the Cluster to be read back from;
- Maintenance Mode, because Apchi engages it itself after a failed Auto Rollback and losing
  that would silently reopen a Cluster whose state is unknown.

What it leaves out is activity. `applies`, `validations` and `discoveries` record what
happened rather than what is configured; including them would make this a database backup,
which is `mongodump`'s job and better at it (#110).

It is one response rather than a stream, and that is measured rather than assumed. On a
Cluster deliberately larger than Apchi is built for -- 150 catalogs, 400 grants, 60 resource
groups -- a Snapshot serialises to about 95 KiB, so 500 of them come to roughly 47 MiB and
serialise in under 200 ms. A Snapshot holds the whole configuration rather than a diff, so
this grows linearly and there is no surprise waiting further out.

**This document is every credential the Cluster has ever used.** Snapshots store actual
secret values rather than references (§12), and they are immutable, so a key rotated away
years ago is still in here and cannot be taken out. Handle it the way a password database is
handled, not the way a config dump is.
"""

import logging
from datetime import UTC, datetime
from typing import Final, Literal

from pydantic import BaseModel, Field

from app.api.errors import Conflict
from app.pipeline.admin_values import AdminStore
from app.pipeline.applies import ApplyRecord, ApplyStore
from app.pipeline.candidate import Candidate, CandidateStore
from app.pipeline.history import HighWater, HighWaterMark
from app.pipeline.maintenance import MaintenanceState, MaintenanceStore
from app.pipeline.snapshots import Snapshot, SnapshotStore
from app.sections.admin import AdminValues
from app.version import VERSION

logger = logging.getLogger(__name__)

#: Named and versioned so a restore can refuse a file that is not one of these, rather
#: than discovering it halfway through writing it back (#114).
FORMAT: Final = "apchi.backup/v1"


class Backup(BaseModel):
    """Everything Apchi's own storage holds, plus enough provenance to judge a restore."""

    format: Literal["apchi.backup/v1"] = FORMAT
    taken_at: datetime
    apchi_version: str = Field(description="The Apchi that wrote this document.")
    cluster_id: str | None = Field(
        description=(
            "The Apchi install this history belongs to, from the high-water mark. Null when "
            "Kubernetes would not answer or no Snapshot has been committed yet; a restore "
            "into a different install is the thing this identifies."
        )
    )
    snapshots_ever: int | None = Field(
        description=(
            "The highest Snapshot number ever committed here. Higher than the last Snapshot "
            "below means this export was itself taken after a loss, and is incomplete."
        )
    )
    snapshots: list[Snapshot] = Field(description="Every Snapshot, oldest first.")
    candidate: Candidate | None = Field(
        description=(
            "The stored Configuration Candidate, including edits staged but never applied. "
            "Null when nothing is stored, in which case the Candidate is derived from the "
            "newest Snapshot and this document already holds it."
        )
    )
    admin_values: AdminValues
    maintenance: MaintenanceState


async def take(
    *,
    applies: ApplyStore,
    snapshots: SnapshotStore,
    candidates: CandidateStore,
    admin: AdminStore,
    maintenance: MaintenanceStore,
    high_water: HighWaterMark,
) -> Backup:
    """One coherent read of Apchi's storage, or a refusal.

    Coherence is bracketed, not locked. Each document is read atomically, so the only
    incoherence available is *between* documents, and only one cross-document invariant
    matters: the Candidate's `base_snapshot` must be a Snapshot this export holds. Reading
    the Candidate before the Snapshots makes that hold by construction -- its base can only
    be older than the newest Snapshot seen afterwards.

    The bracket is for the rest: an Apply in flight is refused before the read and checked
    for again after it, along with the newest Snapshot number. An Apply that began and
    committed during the read moves that number; one that began and failed moves neither it
    nor the Candidate, because a failed Apply commits nothing.

    What the bracket does not cover is an Admin. Editing Admin values while taking your own
    export races with yourself, and no check here would tell you something you do not know.
    """
    _refuse_if_applying(await applies.in_flight())

    candidate = await candidates.stored()
    held = await snapshots.every()
    values = await admin.load()
    mode = await maintenance.get()
    mark = await _mark(high_water)

    _refuse_if_applying(await applies.in_flight())
    newest = await snapshots.latest()
    if (newest.number if newest else None) != (held[-1].number if held else None):
        raise Conflict(
            "An Apply committed a Snapshot while the export was being read. "
            "Nothing was written anywhere; take it again."
        )

    backup = Backup(
        taken_at=datetime.now(UTC),
        apchi_version=VERSION,
        cluster_id=mark.cluster_id if mark else None,
        snapshots_ever=mark.highest_snapshot if mark else None,
        snapshots=held,
        candidate=candidate,
        admin_values=values,
        maintenance=mode,
    )
    logger.info(
        "state exported",
        extra={"snapshots": len(held), "staged": candidate is not None},
    )
    return backup


def _refuse_if_applying(running: ApplyRecord | None) -> None:
    if running is not None:
        raise Conflict(
            f"Apply {running.id} is in flight ({running.stage.value}). An export taken now "
            "could hold a Candidate mid-promotion, so it is refused until the Apply finishes."
        )


async def _mark(high_water: HighWaterMark) -> HighWater | None:
    """The high-water mark, or None if it cannot be read.

    An export without provenance is worth more than no export, and the same reasoning
    applies as at startup: Apchi tolerates a Kubernetes that will not answer, and a
    transient 403 must not cost an Admin their backup.
    """
    try:
        return await high_water.read()
    except Exception:
        logger.warning("could not read the high-water mark for the export", exc_info=True)
        return None

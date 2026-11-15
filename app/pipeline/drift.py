"""Whether the Cluster still holds what Apchi committed to it.

A Snapshot records what Apchi delivered. It does not keep the Cluster that way, and two
things move it (§14):

- someone edits a Secret by hand, or a ConfigMap the Admin owns changes underneath Apchi;
- an Admin value moves, which moves no Section at all and still changes what every
  affected Section renders.

Either way the next Apply writes files nobody staged, and for a rollout-required Section it
restarts the coordinator and terminates every running query to do it. So drift is worth
reading before an Apply rather than discovering inside one.

Nothing here reconciles. Apply does that, and §21's promise is that an Operator sees the
cost before paying it -- a one-button "fix drift" that restarts Trino is that promise
inverted.
"""

import logging
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.pipeline.files import would_change
from app.sections import SectionName
from app.sections.base import Cluster, Resources
from app.sections.registry import REGISTERED

logger = logging.getLogger(__name__)

#: What can be said about one Section. `not_comparable` is not a failure and not a transient
#: state: it is a Section that does not reach Trino as a file, so there is nothing to diff.
Status = Literal["in_sync", "diverged", "unknown", "not_comparable"]


class SectionDrift(BaseModel):
    """One Section's answer."""

    model_config = ConfigDict(extra="forbid")

    section: SectionName
    status: Status = Field(
        description=(
            "`in_sync` when what Apchi would render is byte-identical to what the Cluster "
            "holds. `diverged` when it is not. `unknown` when the Cluster could not be "
            "read. `not_comparable` when this Section does not reach Trino as a file at "
            "all -- Catalogs are delivered as statements, so no file diff can speak for "
            "them, and claiming they are in sync would be an answer Apchi has not earned."
        )
    )
    restarts_coordinator: bool = Field(
        description=(
            "Whether an Apply that reconciles this Section restarts the coordinator. Here "
            "rather than in a client-side table of Section names, for the reason #210 put "
            "`rollout_sections` on the Apply cost: the table is a second copy of Apchi's "
            "own registry, and it goes stale silently."
        )
    )


class Drift(BaseModel):
    """One reading, taken when asked."""

    model_config = ConfigDict(extra="forbid")

    at: datetime = Field(
        description="When this reading was taken. Already stale by the time it is read."
    )
    snapshot: int | None = Field(
        description=(
            "The committed Snapshot this is measured against; null when nothing has been "
            "applied yet, which is the one case where every Section reads `unknown` -- "
            "there is no committed state to have drifted from."
        )
    )
    diverged: bool | None = Field(
        description=(
            "True when at least one Section is known to have drifted. Null when none is but "
            "at least one could not be read, because an all-clear Apchi could not verify is "
            "not an all-clear. False means every Section Apchi *can* compare is in sync, and "
            "says nothing about a `not_comparable` one -- `sections` carries that."
        )
    )
    sections: list[SectionDrift] = Field(
        description="Every registered Section, in Section order, whether it drifted or not."
    )


def _verdict(sections: list[SectionDrift]) -> bool | None:
    """The roll-up, which a known divergence wins and an unreadable Section spoils.

    `not_comparable` is deliberately not counted. It is permanent -- Catalogs will never be
    a file -- so letting it reach this would pin the verdict at null forever and make the
    field worth nothing to the indicator that reads it.
    """
    if any(section.status == "diverged" for section in sections):
        return True
    if any(section.status == "unknown" for section in sections):
        return None
    return False


async def drift_of(
    cluster: Cluster,
    snapshot: int | None,
    committed: dict[SectionName, Resources],
) -> Drift:
    """Compare every Section's rendered files against what the Cluster has mounted.

    Rendered with the Admin values in force *now*, not the ones that were in force at the
    Snapshot -- which is what makes an Admin change show up here as drift, and is the point:
    the question is what an Apply would write today, not what yesterday's Apply wrote.
    """
    at = datetime.now(UTC)
    if snapshot is None:
        return Drift(
            at=at,
            snapshot=None,
            diverged=None,
            sections=[
                _undetermined(section.name, section.requires_rollout) for section in REGISTERED
            ],
        )

    sections: list[SectionDrift] = []
    for section in REGISTERED:
        status: Status
        if not section.coordinator_files(cluster.settings):
            status = "not_comparable"
        else:
            try:
                status = (
                    "diverged"
                    if await would_change(section, cluster, committed.get(section.name, {}))
                    else "in_sync"
                )
            except Exception:
                # Per Section rather than failing the read, and the opposite of the swallowed
                # error the convention warns about: the Section is reported `unknown`, so a
                # caller is told what could not be answered instead of being handed a
                # success. One Section's Secret failing must not cost the other five.
                logger.warning(
                    "could not read what the Cluster holds for %s",
                    section.name,
                    exc_info=True,
                )
                status = "unknown"
        sections.append(
            SectionDrift(
                section=section.name,
                status=status,
                restarts_coordinator=section.requires_rollout,
            )
        )

    return Drift(at=at, snapshot=snapshot, diverged=_verdict(sections), sections=sections)


def _undetermined(name: SectionName, restarts: bool) -> SectionDrift:
    return SectionDrift(section=name, status="unknown", restarts_coordinator=restarts)

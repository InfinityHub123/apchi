"""Review and Reset: seeing what is staged, and throwing it away."""

from typing import Any, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.api.deps import (
    CandidateStoreDep,
    ClusterDep,
    OperatorMutationAllowed,
    SnapshotStoreDep,
)
from app.pipeline.impact import ApplyCost, cost_of
from app.pipeline.recovery import RevertEffect, RevertRequest, full_rollback
from app.sections import SectionName
from app.sections.registry import REGISTERED, SECTIONS

router = APIRouter(tags=["candidate"])

ChangeKind = Literal["added", "changed", "removed"]


#: What one resource looks like in a diff. A Section stores a resource as whatever shape
#: that Section needs, and the Resource Groups Section stores its whole ordered selector
#: list as one resource -- order is meaning there, so the list cannot be split into one
#: resource per selector. A diff entry therefore has to carry a list as readily as an object.
ResourceBody = dict[str, Any] | list[Any]


class ResourceChange(BaseModel):
    resource: str
    change: ChangeKind
    before: ResourceBody | None = None
    after: ResourceBody | None = None


class SectionDiff(BaseModel):
    section: SectionName
    changes: list[ResourceChange] = Field(default_factory=list)


class Review(BaseModel):
    """The diff between the Configuration Candidate and the latest Snapshot.

    Every Section is reported, not only the caller's changes: the Candidate is
    shared, and an Apply promotes everything in it.
    """

    base_snapshot: int | None = Field(
        description="The Snapshot this Candidate was derived from; null before the first."
    )
    has_changes: bool
    sections: list[SectionDiff]
    cost: ApplyCost = Field(
        description="What applying the Candidate as it stands would cost the Cluster."
    )


def _diff(before: dict[str, Any], after: dict[str, Any]) -> list[ResourceChange]:
    changes: list[ResourceChange] = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if old is None:
            changes.append(ResourceChange(resource=name, change="added", after=new))
        elif new is None:
            changes.append(ResourceChange(resource=name, change="removed", before=old))
        else:
            changes.append(ResourceChange(resource=name, change="changed", before=old, after=new))
    return changes


@router.get("/review", response_model=Review, summary="What an Apply would change")
async def review(
    store: CandidateStoreDep, snapshots: SnapshotStoreDep, cluster: ClusterDep
) -> Review:
    candidate = await store.load()
    # Diffed against the Snapshot the Candidate was derived from, so Review answers
    # "what would this Apply change" rather than "what is staged". Before the first
    # Snapshot the baseline is empty and everything staged reads as added.
    baseline = await snapshots.sections_of(candidate.base_snapshot)

    sections = [
        SectionDiff(section=name, changes=_diff(baseline[name], candidate.sections.get(name, {})))
        for name in SECTIONS
    ]
    # Planned with the Sections' own planners rather than read off the diff above: the
    # restart decision has to be the one Apply will make, not a second implementation of it.
    plans = {
        section.name: section.plan(candidate.sections.get(section.name, {}), baseline[section.name])
        for section in REGISTERED
    }

    return Review(
        base_snapshot=candidate.base_snapshot,
        has_changes=any(section.changes for section in sections),
        sections=sections,
        cost=await cost_of(plans, candidate.sections, cluster),
    )


@router.post(
    "/candidate/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Discard everything staged",
    dependencies=[OperatorMutationAllowed],
)
async def reset(store: CandidateStoreDep) -> None:
    """Re-derives the Candidate from the latest Snapshot.

    Because nothing reaches the Cluster before Apply, this cannot affect production.
    """
    current = await store.load()
    await store.reset(base_snapshot=current.base_snapshot)


@router.post(
    "/candidate/rollback",
    response_model=RevertEffect,
    summary="Stage an earlier Snapshot in its entirety",
    dependencies=[OperatorMutationAllowed],
)
async def rollback(
    request: RevertRequest,
    store: CandidateStoreDep,
    snapshots: SnapshotStoreDep,
    cluster: ClusterDep,
) -> RevertEffect:
    """Full Rollback: for a serious mistake, and never implicit.

    Like a Section Revert this only stages. The Snapshot being restored from is not
    modified -- applying this produces a new one, so history is never rewritten.
    """
    return await full_rollback(store, snapshots, cluster, request.snapshot)

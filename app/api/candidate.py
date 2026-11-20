"""Review and Reset: seeing what is staged, and throwing it away."""

import logging
from typing import Any, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import (
    CandidateStoreDep,
    ClusterDep,
    OperatorMutationAllowed,
    SnapshotStoreDep,
)
from app.pipeline.configuration import Configuration, Import, accepted, of_candidate
from app.pipeline.impact import ApplyCost, cost_of
from app.pipeline.recovery import RevertEffect, RevertRequest, full_rollback
from app.sections import SectionName
from app.sections.registry import REGISTERED, SECTIONS

logger = logging.getLogger(__name__)

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


class ResourceReset(BaseModel):
    """Which one resource to Reset."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"section": "catalogs", "resource": "finance_pg"}]},
    )

    section: SectionName
    resource: str = Field(
        description=(
            "The resource's name exactly as Review reports it in `changes[].resource`. In "
            "the body rather than the path because some of those names cannot travel in a "
            "path: Resource Groups keys its selector list as `#selectors`, and `#` in a URL "
            "starts the fragment, so the server would receive a truncated name."
        )
    )


@router.post(
    "/candidate/reset-resource",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Reset one staged change, leaving every other one",
    dependencies=[OperatorMutationAllowed],
)
async def reset_resource(request: ResourceReset, store: CandidateStoreDep) -> None:
    """Undo one resource's staged change: it goes back as the base Snapshot has it.

    An edit gets the Snapshot's version back, a removal is restored, an addition is removed.
    Every other staged change stays -- in this Section and every other, including other
    people's. Before this the smallest unit was a Section Revert, which discards everyone's
    edits to the Section along with the one being undone.

    A separate route rather than an optional body on `POST /candidate/reset`, deliberately.
    That one discards **everything**, and a client that forgot the body would get it.

    Works for Client Certificates too, which re-sending a Snapshot's version cannot do from
    outside: their upload takes a ZIP, and the Snapshot's copy never leaves Apchi.

    404 when the resource has nothing staged, rather than a silent success that would read
    as "undone" to somebody who typed the wrong name. Recorded in the audit as a `reset` with
    the resource named, so it cannot be mistaken for discarding everything.
    """
    await store.reset_resource(request.section, request.resource)


@router.post(
    "/candidate/rollback",
    response_model=RevertEffect,
    summary="Full Rollback: revert every Section to one Snapshot",
    dependencies=[OperatorMutationAllowed],
)
async def rollback(
    request: RevertRequest,
    store: CandidateStoreDep,
    snapshots: SnapshotStoreDep,
    cluster: ClusterDep,
) -> RevertEffect:
    """Full Rollback: for a serious mistake, and never implicit.

    Replaces **all six Sections** at once, which is what distinguishes this from the
    per-Section `POST /{section}/revert`: those leave the other Sections where they are, and
    the result is a combination that has never run anywhere.

    Like a Section Revert this only stages -- an ordinary `POST /applies` follows. The
    Snapshot being restored from is not modified; applying this produces a new one, so
    history is never rewritten.

    **Admin values are not included.** Raw files, permission enforcement and the preserved
    mapping patterns are the Admin's and never enter a Snapshot (invariant 9), so a Full
    Rollback does not restore them. Nor does it reach outside Apchi: a Snapshot naming a
    database that has since moved restores the configuration, not the world it described.
    """
    return await full_rollback(store, snapshots, cluster, request.snapshot)


@router.get(
    "/candidate/export",
    response_model=Configuration,
    summary="What is staged, as a portable configuration",
)
async def export_candidate(store: CandidateStoreDep) -> Configuration:
    """Staged work, which is in no Snapshot.

    Exported as well as Snapshots because this is the case "I was working on this and want
    it elsewhere" actually needs: an edit that has not been applied has never been
    committed, so no Snapshot holds it.

    **The response holds real credentials**, for the same reason a Snapshot export does.
    """
    candidate = await store.load()
    return of_candidate(candidate.base_snapshot, candidate.sections)


@router.put(
    "/candidate",
    response_model=Review,
    summary="Replace everything staged with an imported configuration",
    dependencies=[OperatorMutationAllowed],
)
async def import_candidate(
    document: Import,
    store: CandidateStoreDep,
    snapshots: SnapshotStoreDep,
    cluster: ClusterDep,
) -> Review:
    """PUT because it replaces the whole resource, which is what an import is.

    An import is not a Reset. `POST /candidate/reset` re-derives from a Snapshot and so
    always leaves an empty diff; this replaces from a file and is expected to leave a large
    one. Both discard staged edits, so both take the same gate.

    Rejected whole, naming every problem, before anything is stored: the Candidate is
    shared, so one invalid resource in it would block everyone's Apply and not just the
    importer's (ADR-0002).

    Nothing reaches the Cluster. The response is the Review of what an Apply would now
    change -- diffed against this Cluster's latest Snapshot, because that is the change
    anyone here is about to make.
    """
    candidate = await store.replace(accepted(document))
    logger.info(
        "configuration imported",
        extra={
            "base_snapshot": candidate.base_snapshot,
            "sections": {name: len(candidate.sections[name]) for name in SECTIONS},
        },
    )
    return await review(store, snapshots, cluster)

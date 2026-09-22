"""Review and Reset: seeing what is staged, and throwing it away."""

from typing import Any, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.api.deps import CandidateStoreDep, CandidateUnfrozen
from app.sections import SECTIONS, SectionName

router = APIRouter(tags=["candidate"])

ChangeKind = Literal["added", "changed", "removed"]


class ResourceChange(BaseModel):
    resource: str
    change: ChangeKind
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


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
async def review(store: CandidateStoreDep) -> Review:
    candidate = await store.load()
    # Until Commit exists there is no Snapshot, so the Candidate is diffed against
    # an empty baseline: everything staged reads as added.
    baseline: dict[SectionName, dict[str, Any]] = {name: {} for name in SECTIONS}

    sections = [
        SectionDiff(section=name, changes=_diff(baseline[name], candidate.sections.get(name, {})))
        for name in SECTIONS
    ]
    return Review(
        base_snapshot=candidate.base_snapshot,
        has_changes=any(section.changes for section in sections),
        sections=sections,
    )


@router.post(
    "/candidate/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Discard everything staged",
    dependencies=[CandidateUnfrozen],
)
async def reset(store: CandidateStoreDep) -> None:
    """Re-derives the Candidate from the latest Snapshot.

    Because nothing reaches the Cluster before Apply, this cannot affect production.
    """
    current = await store.load()
    await store.reset(base_snapshot=current.base_snapshot)

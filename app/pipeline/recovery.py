"""Section Revert and Full Rollback: staging an earlier Snapshot into the Candidate.

Neither of these touches the Cluster. They stage, and then need an ordinary Apply
like any other edit -- which is what keeps the rule that nothing bypasses the
pipeline, and lets an Operator revert one Section and adjust another before applying
once. Both therefore produce a **new** Snapshot; the one being restored from is never
modified. See section 11.

The distinction worth keeping straight is what the Operator ends up with. A Section
Revert takes one Section back and leaves the others where they are, which is a
combination that has never run anywhere. A Full Rollback takes everything back
together. Neither is "a return to a known-good state", and the effect these functions
return says so in words rather than leaving the UI to imply otherwise.
"""

import logging

from pydantic import BaseModel, Field

from app.api.errors import NotFound
from app.pipeline.candidate import Candidate, CandidateStore
from app.pipeline.snapshots import SnapshotStore
from app.sections import SECTIONS, SectionName
from app.sections.catalogs import apply as catalog_apply
from app.sections.catalogs.section import SECTION as CATALOGS

logger = logging.getLogger(__name__)


class RevertEffect(BaseModel):
    """What was staged, and what an Apply would then do to the Cluster.

    The catalog lists are the point of this model. A revert is not a paper change:
    applying it issues real DROP CATALOG statements, and "3 catalogs will be dropped"
    is a materially different warning from "the configuration will be rewritten".
    """

    from_snapshot: int
    sections: list[SectionName] = Field(description="The Sections replaced in the Candidate.")
    other_sections_stay_at: int | None = Field(
        default=None,
        description="The Snapshot every other Section remains at. Null for a Full Rollback.",
    )
    catalogs_dropped: list[str] = Field(
        default_factory=list, description="Catalogs an Apply will DROP. Destructive."
    )
    catalogs_created: list[str] = Field(default_factory=list)
    catalogs_replaced: list[str] = Field(default_factory=list)
    summary: str = Field(description="What this does, in words, for an Operator to read.")


async def _snapshot_sections(
    snapshots: SnapshotStore, number: int
) -> dict[SectionName, dict[str, object]]:
    if await snapshots.get(number) is None:
        raise NotFound(f"No Snapshot numbered {number}.")
    return await snapshots.sections_of(number)


async def _effect(
    candidate: Candidate,
    snapshots: SnapshotStore,
    number: int,
    sections: list[SectionName],
    others_stay_at: int | None,
    summary_for: str,
) -> RevertEffect:
    """The Apply that would follow, planned but not run.

    Planned against the Candidate's base Snapshot, because that is what Apply itself
    plans against -- so these are the statements an Operator would actually cause.
    """
    baseline = await snapshots.sections_of(candidate.base_snapshot)
    plan = catalog_apply.plan(candidate.sections.get(CATALOGS, {}), baseline.get(CATALOGS, {}))
    effect = RevertEffect(
        from_snapshot=number,
        sections=sections,
        # Stated by the caller, never inferred from how many Sections were replaced:
        # while catalogs is the only Section, a revert of it covers all of them and is
        # still not a Full Rollback.
        other_sections_stay_at=others_stay_at,
        catalogs_dropped=plan.dropped,
        catalogs_created=plan.created,
        catalogs_replaced=plan.replaced,
        summary="",
    )
    effect.summary = _summary(effect, summary_for)
    return effect


def _summary(effect: RevertEffect, kind: str) -> str:
    parts = [kind]
    if effect.catalogs_dropped:
        # Named, not counted. An Operator about to lose a catalog should see which one.
        parts.append(
            f"Applying this will drop {len(effect.catalogs_dropped)} "
            f"catalog{'s' if len(effect.catalogs_dropped) > 1 else ''}: "
            f"{', '.join(effect.catalogs_dropped)}."
        )
    else:
        parts.append("Applying this will drop no catalogs.")
    parts.append(
        f"Snapshot {effect.from_snapshot} is not modified; applying this produces a new Snapshot."
    )
    return " ".join(parts)


async def section_revert(
    candidates: CandidateStore, snapshots: SnapshotStore, section: SectionName, number: int
) -> RevertEffect:
    """Replace one Section of the Candidate with its content from an earlier Snapshot."""
    restored = await _snapshot_sections(snapshots, number)
    candidate = await candidates.load()
    candidate.sections[section] = dict(restored.get(section, {}))
    await candidates.save(candidate)

    effect = await _effect(
        candidate,
        snapshots,
        number,
        [section],
        candidate.base_snapshot,
        f"{section} will be restored to Snapshot {number}; every other Section stays at "
        f"Snapshot {candidate.base_snapshot}. That combination has never run.",
    )
    logger.info(
        "section reverted",
        extra={"section": section, "from_snapshot": number, "drops": len(effect.catalogs_dropped)},
    )
    return effect


async def full_rollback(
    candidates: CandidateStore, snapshots: SnapshotStore, number: int
) -> RevertEffect:
    """Replace the entire Candidate with an earlier Snapshot.

    Reserved for a serious mistake, and never implicit: only an Operator asking for it
    by number gets it.
    """
    restored = await _snapshot_sections(snapshots, number)
    candidate = await candidates.load()
    candidate.sections = {name: dict(restored.get(name, {})) for name in SECTIONS}
    await candidates.save(candidate)

    effect = await _effect(
        candidate,
        snapshots,
        number,
        list(SECTIONS),
        None,
        f"The entire Candidate will be replaced with Snapshot {number}.",
    )
    logger.warning(
        "full rollback staged",
        extra={"from_snapshot": number, "drops": len(effect.catalogs_dropped)},
    )
    return effect

"""What applying the Candidate would cost the Cluster.

Only one cost is worth stating, and it is a large one. A Rollout terminates every running
and queued query: Trino cannot drain a coordinator -- `NodeStateManager.transitionState()`
throws for one and graceful shutdown is documented as workers-only -- and there is no
coordinator HA. No configuration of Apchi changes that, so Apchi says so before an Operator
commits to it rather than pretending to mitigate it. See section 7.3 and ADR-0003.
"""

import logging

from pydantic import BaseModel, Field

from app.pipeline.files import would_change
from app.sections import SectionName
from app.sections.base import Cluster, Resources, SectionPlan
from app.sections.registry import REGISTERED

logger = logging.getLogger(__name__)

#: In those words, because "the cluster will restart" does not tell an Operator what it
#: costs them.
RESTART_WARNING = (
    "Applying this restarts the Trino coordinator, which terminates every running and "
    "queued query. Trino cannot drain a coordinator, so there is no way to avoid it."
)


class ApplyCost(BaseModel):
    """What an Operator is agreeing to when they Apply."""

    restarts_coordinator: bool = Field(
        description="Whether applying the Candidate as it stands requires a Rollout."
    )
    rollout_sections: list[SectionName] = Field(
        default_factory=list,
        description=(
            "Which Sections the Rollout is for, in Section order. §21 requires an Operator "
            "to see which changes force a restart and which do not, and a client cannot "
            "work it out from `Section.requires_rollout`: a Section also forces a Rollout "
            "when what Apchi would render is not what the Cluster has mounted. A name here "
            "with no entry in the diff is a Rollout that no staged change accounts for."
        ),
    )
    queries_at_risk: int | None = Field(
        default=None,
        description=(
            "Queries running or queued on the Cluster right now, every one of which a "
            "Rollout terminates. A live reading and not a promise: it is already stale, "
            "and it is null when the Cluster could not be asked or when no restart is "
            "coming."
        ),
    )
    warning: str | None = Field(
        default=None, description="What to show an Operator before they Apply."
    )


async def sections_needing_rollout(
    plans: dict[SectionName, SectionPlan],
    desired: dict[SectionName, Resources],
    cluster: Cluster,
) -> set[SectionName]:
    """Which rollout-required Sections a change actually moves.

    Two questions, because there are two ways what Apchi would write can differ from what
    Trino is running. The plan answers the Operator's: the Candidate moved. The Cluster
    answers the other: the rendered file is not the mounted one -- an Admin value changed
    (§14), or someone edited the Secret by hand. The second is a diff of two durable
    records, so it needs no history of its own and survives an Apchi restart.

    A Section that needs no restart never causes one, and a rollout-required Section that
    did not change does not either -- otherwise every Apply on a Cluster with an Event
    Listener configured would terminate every running query for nothing.

    An unreachable Cluster falls back to the plan. Refusing because the decision could not
    be *double*-checked would be worse than answering as Apchi always has.
    """
    changed: set[SectionName] = set()
    for section in REGISTERED:
        if not section.requires_rollout:
            continue
        plan = plans.get(section.name)
        if plan is not None and not plan.empty:
            changed.add(section.name)
            continue
        try:
            if await would_change(section, cluster, desired.get(section.name, {})):
                changed.add(section.name)
        except Exception:
            logger.warning(
                "could not read what the Cluster holds for %s; the restart decision "
                "falls back to the plan",
                section.name,
                exc_info=True,
            )
    return changed


async def cost_of(
    plans: dict[SectionName, SectionPlan],
    desired: dict[SectionName, Resources],
    cluster: Cluster,
) -> ApplyCost:
    """The Cluster is only asked how busy it is when the answer matters.

    A Candidate that needs no restart costs no queries, so there is nothing to count.
    """
    rollout = await sections_needing_rollout(plans, desired, cluster)
    if not rollout:
        return ApplyCost(restarts_coordinator=False)

    try:
        at_risk: int | None = await cluster.trino.queries_at_risk()
    except Exception:
        # Review's job is to show what is staged. A Cluster that cannot be asked how busy
        # it is must not stop an Operator seeing their own changes.
        logger.warning("could not count queries at risk", exc_info=True)
        at_risk = None

    return ApplyCost(
        restarts_coordinator=True,
        # Registry order rather than the set's, so Review and this list name the Sections in
        # the same order and a client can join them without sorting either.
        rollout_sections=[s.name for s in REGISTERED if s.name in rollout],
        queries_at_risk=at_risk,
        warning=RESTART_WARNING,
    )

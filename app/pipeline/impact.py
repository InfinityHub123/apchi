"""What applying the Candidate would cost the Cluster.

Only one cost is worth stating, and it is a large one. A Rollout terminates every running
and queued query: Trino cannot drain a coordinator -- `NodeStateManager.transitionState()`
throws for one and graceful shutdown is documented as workers-only -- and there is no
coordinator HA. No configuration of Apchi changes that, so Apchi says so before an Operator
commits to it rather than pretending to mitigate it. See section 7.3 and ADR-0003.
"""

import logging

from pydantic import BaseModel, Field

from app.adapters.trino import Trino
from app.sections import SectionName
from app.sections.base import SectionPlan
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


def restarts_coordinator(plans: dict[SectionName, SectionPlan]) -> bool:
    """Whether these changes need Trino restarted.

    The same rule Apply itself follows: a Section that needs no restart never causes one,
    and a rollout-required Section that did not change does not either.
    """
    return any(
        section.requires_rollout
        and (plan := plans.get(section.name)) is not None
        and not plan.empty
        for section in REGISTERED
    )


async def cost_of(plans: dict[SectionName, SectionPlan], trino: Trino) -> ApplyCost:
    """The Cluster is only asked when the answer matters.

    A Candidate that needs no restart costs no queries, so there is nothing to count and no
    reason to make Review depend on the Cluster being reachable.
    """
    if not restarts_coordinator(plans):
        return ApplyCost(restarts_coordinator=False)

    try:
        at_risk: int | None = await trino.queries_at_risk()
    except Exception:
        # Review's job is to show what is staged. A Cluster that cannot be asked how busy
        # it is must not stop an Operator seeing their own changes.
        logger.warning("could not count queries at risk", exc_info=True)
        at_risk = None

    return ApplyCost(restarts_coordinator=True, queries_at_risk=at_risk, warning=RESTART_WARNING)

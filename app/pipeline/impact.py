"""What applying the Candidate would cost the Cluster.

Only one cost is worth stating, and it is a large one. A Rollout terminates every running
and queued query: Trino cannot drain a coordinator -- `NodeStateManager.transitionState()`
throws for one and graceful shutdown is documented as workers-only -- and there is no
coordinator HA. No configuration of Apchi changes that, so Apchi says so before an Operator
commits to it rather than pretending to mitigate it. See section 7.3 and ADR-0003.
"""

import logging

from pydantic import BaseModel, Field

from app.pipeline import checksum, raw_files, server_configuration
from app.pipeline.files import would_change
from app.sections import SectionName
from app.sections.base import Cluster, Resources, SectionPlan
from app.sections.registry import REGISTERED
from app.sections.server_configuration import Role

logger = logging.getLogger(__name__)

#: In those words, because "the cluster will restart" does not tell an Operator what it
#: costs them.
RESTART_WARNING = (
    "Applying this restarts the Trino coordinator, which terminates every running and "
    "queued query. Trino cannot drain a coordinator, so there is no way to avoid it."
)

#: The workers' restart, which costs no query: each one drains before it goes.
WORKER_RESTART_WARNING = (
    "Applying this restarts the Trino workers one at a time. Each finishes its running tasks "
    "before it stops, so no query is lost, but the Cluster runs one worker short while each "
    "is replaced."
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
    restarts_workers: bool = Field(
        default=False,
        description=(
            "Whether it restarts the workers, which happens only when their own server "
            "configuration changes. They drain first, so this costs no query."
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
    rewired: bool = Field(
        default=False,
        description=(
            "Whether Trino's own files on the Cluster differ from what the Admin values last "
            "applied render to. Nothing an Operator or an Admin staged moved them: the chart "
            "changed what it wires -- `trino.https.*`, a port, a mount -- or someone edited "
            "the Secret by hand. The pods run the old files until an Apply delivers, and an "
            "Admin Apply does that without the Candidate (POST /api/v1/admin/applies)."
        ),
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

    Every Apply delivers the Admin's files too, so a Server Configuration or Raw File not yet
    applied -- or wiring the chart changed -- restarts the coordinator at the next Apply,
    whoever starts it. An Operator is told before they commit to that, not after.
    """
    rollout = await sections_needing_rollout(plans, desired, cluster)
    coordinator_files, workers = await _admin_files_move(cluster)
    rewired = await _rewired(cluster)
    behind = await _behind(cluster, desired)
    coordinator_files = coordinator_files or "coordinator" in behind
    workers = workers or "worker" in behind
    if not rollout and not coordinator_files:
        # The workers drain, so their restart puts no query at risk and is nothing to count.
        return ApplyCost(
            restarts_coordinator=False,
            restarts_workers=workers,
            warning=WORKER_RESTART_WARNING if workers else None,
            rewired=rewired,
        )

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
        restarts_workers=workers,
        queries_at_risk=at_risk,
        warning=RESTART_WARNING,
        rewired=rewired,
    )


async def _admin_files_move(cluster: Cluster) -> tuple[bool, bool]:
    """Whether the next Apply would rewrite the Admin's files: (on the coordinator, on the
    workers). Raw files are mounted on the coordinator alone.

    An unreadable Cluster answers no, as `sections_needing_rollout` does for a Section: Review
    must still show what is staged when Kubernetes cannot be asked.
    """
    try:
        roles = await server_configuration.changed_roles(cluster)
        coordinator = "coordinator" in roles or await raw_files.changed(cluster)
        return coordinator, "worker" in roles
    except Exception:
        logger.warning(
            "could not read the Admin's files on the coordinator; the restart decision "
            "leaves them out",
            exc_info=True,
        )
        return False, False


async def _rewired(cluster: Cluster) -> bool:
    """Whether the files moved under the Admin values last applied. Unreadable, or with no
    record of those values, answers no, as the checks above do."""
    if cluster.admin_applied is None:
        return False
    try:
        return bool(await server_configuration.rewired_roles(cluster, cluster.admin_applied))
    except Exception:
        logger.warning(
            "could not read Trino's files on the Cluster; Review leaves out whether the "
            "chart rewired them",
            exc_info=True,
        )
        return False


async def _behind(cluster: Cluster, desired: dict[SectionName, Resources]) -> set[Role]:
    """The roles an Apply would restart because their pods started from other files.

    The same question the Apply asks, so Review and the Apply cannot disagree about a
    restart. Unreadable answers none, as the two checks above do.
    """
    try:
        return await checksum.behind(cluster, desired)
    except Exception:
        logger.warning(
            "could not read what Trino was started from; the restart decision leaves it out",
            exc_info=True,
        )
        return set()

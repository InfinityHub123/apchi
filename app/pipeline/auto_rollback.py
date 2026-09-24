"""Auto Rollback: one bounded attempt to put the Cluster back to its latest Snapshot.

Despite the name it is not a Full Rollback. It is automatic rather than an Operator
action, it creates no Snapshot, and the latest-Snapshot pointer never moves. It runs
after an Apply or Verification failure -- never after a Validation failure, where
nothing was touched, and never after a Commit failure, where the Cluster is running
a configuration that was applied and verified and the thing that broke was MongoDB.

It attempts Verification **once** and never retries. Two consecutive verification
failures mean the problem is not the configuration: that is an operational incident,
not something more DDL will fix. See section 10.
"""

import logging

import httpx

from app.config import Settings
from app.pipeline.maintenance import EngagedBy, MaintenanceStore
from app.pipeline.rollout import roll_out
from app.pipeline.verification import verify
from app.sections import SectionName
from app.sections.base import Cluster, Resources
from app.sections.registry import REGISTERED

logger = logging.getLogger(__name__)

#: What the UI shows after a failed Auto Rollback. Section 10 fixes the wording: it
#: tells an Operator the one thing they can act on, and does not invite them to try
#: again against a Cluster in an unknown state.
INCIDENT_MESSAGE = (
    "The Trino cluster is currently unhealthy after applying configuration. "
    "Please contact our team."
)


class RollbackFailed(Exception):
    """The single attempt did not restore the Cluster. Nothing else is tried."""


async def restore(
    snapshot_sections: dict[SectionName, Resources],
    cluster: Cluster,
) -> None:
    """Put every Section back to the latest Snapshot, then verify once.

    Each Section knows how to undo itself, including what it can and cannot assume about
    how far a failed Apply got. The pipeline only decides that this happens once.
    """
    changed = {
        section.name
        for section in REGISTERED
        if await section.restore(cluster, snapshot_sections.get(section.name, {}))
    }

    # A Section Trino adopts only by restarting has to be restarted to be *un*done too,
    # and that restart is part of the single bounded attempt rather than a retry.
    if any(section.requires_rollout and section.name in changed for section in REGISTERED):
        await roll_out(
            cluster.kubernetes,
            cluster.settings.coordinator_deployment_name,
            "auto rollback to the latest Snapshot",
            cluster.settings.rollout_timeout_seconds,
        )

    await verify(
        cluster,
        snapshot_sections,
        cluster.settings.worker_deployment_name,
        cluster.settings.verification_catalog,
    )
    logger.info("auto rollback restored the latest Snapshot")


async def declare_incident(
    apply_id: str, reason: str, maintenance: MaintenanceStore, settings: Settings
) -> None:
    """Stop touching the Cluster and say so loudly.

    Maintenance Mode engages first. It is the part that protects the Cluster, and it
    must not be left waiting on an alert to a system that may itself be down.
    """
    await maintenance.engage(
        f"Auto Rollback failed after Apply {apply_id}: {reason}",
        EngagedBy.AUTO_ROLLBACK_FAILURE,
    )
    logger.critical(
        "auto rollback failed; maintenance mode engaged",
        extra={"failed_apply": apply_id, "why": reason},
    )
    await _alert(apply_id, reason, settings)


async def _alert(apply_id: str, reason: str, settings: Settings) -> None:
    """Notify whoever operates Apchi. A failure to alert is logged and never raised:
    it must not mask the incident it was trying to report."""
    if not settings.alert_webhook_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                settings.alert_webhook_url,
                json={
                    "text": (
                        f"Apchi: Auto Rollback failed after Apply {apply_id} on "
                        f"{settings.environment.value}. Maintenance Mode is engaged and the "
                        f"cluster state is unknown. Reason: {reason}"
                    )
                },
            )
            response.raise_for_status()
    except Exception:
        logger.exception("could not deliver the incident alert")

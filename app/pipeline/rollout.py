"""Rollout: restarting Trino so it adopts configuration it cannot adopt while running.

One mechanism of Apply, not a thing an Operator asks for directly. **It terminates every
running and queued query.** Trino cannot drain a coordinator --
`NodeStateManager.transitionState()` throws for one, and graceful shutdown is documented as
usable exclusively on workers -- and there is no coordinator HA. No configuration of Apchi
changes that. See section 7.3 and ADR-0003.

The wait is bounded and polled rather than slept: a fixed duration is both usually wrong
and occasionally too short.
"""

import asyncio
import logging
import time

from app.adapters.kubernetes import KubernetesAdapter

logger = logging.getLogger(__name__)

#: How often the Deployment is asked whether it has finished. Short enough that a fast
#: rollout is not padded, long enough not to hammer the API server for ten minutes.
_POLL_SECONDS = 2.0


class RolloutFailed(Exception):
    """The Cluster did not come back within the timeout."""


async def roll_out(
    kubernetes: KubernetesAdapter, deployment: str, reason: str, timeout: float
) -> None:
    """Restart `deployment` and return once every replica is running the new template.

    Waiting for readiness alone would return while pods from the previous template were
    still serving, and Verification would then be asking the old configuration whether it
    had adopted the new one.
    """
    await kubernetes.restart_deployment(deployment, reason)
    logger.warning(
        "rolling out the coordinator; every running query is terminated",
        extra={"deployment": deployment, "why": reason},
    )

    deadline = time.monotonic() + timeout
    state = await kubernetes.rollout_state(deployment)
    while True:
        if state.complete:
            logger.info("rollout complete", extra={"deployment": deployment})
            return
        if time.monotonic() >= deadline:
            raise RolloutFailed(
                f"{deployment} did not finish rolling out within {timeout:.0f}s "
                f"({state.summary()}). The configuration was applied but the Cluster has "
                "not come back on it."
            )
        await asyncio.sleep(_POLL_SECONDS)
        state = await kubernetes.rollout_state(deployment)

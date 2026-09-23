"""Verification: did the Cluster adopt the configuration and stay healthy?

A different question from Validation, which asks whether the configuration should
be applied at all. Verification runs after Apply, against the real Cluster.

It is functional rather than introspective because Trino exposes no endpoint
reporting which configuration is live. Checking that the process restarted proves
nothing about whether it is running what was asked for.
"""

import logging
from typing import Any

from app.adapters.kubernetes import KubernetesAdapter
from app.adapters.trino import Trino
from app.sections import SectionName
from app.sections.catalogs.section import SECTION

logger = logging.getLogger(__name__)


class VerificationFailed(Exception):
    """The Cluster did not adopt the configuration, or is not healthy."""


async def verify(
    trino: Trino,
    kubernetes: KubernetesAdapter,
    desired: dict[SectionName, dict[str, Any]],
    worker_deployment: str,
    verification_catalog: str,
) -> None:
    # 1. The coordinator is responding and past its own startup.
    starting = await trino.is_starting()
    if starting is None:
        raise VerificationFailed("The Trino coordinator did not respond.")
    if starting:
        raise VerificationFailed("The Trino coordinator is still starting.")

    # 2. Workers have registered. The readiness probe only proves the JVM booted --
    #    a coordinator with zero workers passes it. The expectation comes from
    #    Kubernetes so it adjusts when an Admin scales the Cluster, rather than
    #    drifting against a number configured in Apchi.
    expected = await kubernetes.ready_replicas(worker_deployment)
    actual = await trino.active_worker_count()
    if actual < expected:
        raise VerificationFailed(
            f"Only {actual} of {expected} workers have registered with the coordinator."
        )

    # 3. The Cluster is running the catalogs the Candidate asked for. This is what
    #    catches divergence between the Secret and Trino's store while the Apply is
    #    still in flight, rather than leaving it to surface at the next restart.
    wanted = set(desired.get(SECTION, {}))
    live = await trino.catalogs()
    missing = sorted(wanted - live)
    if missing:
        raise VerificationFailed(
            f"The coordinator is not serving {', '.join(missing)}: "
            "the configuration was applied but is not live."
        )

    # 4. The Cluster can actually serve work. "Healthy" should not mean an endpoint
    #    returned 200.
    try:
        await trino.query(f'SELECT 1 FROM "{verification_catalog}".runtime.nodes LIMIT 1')
    except Exception as exc:
        raise VerificationFailed(f"The smoke query failed: {exc}") from exc

    logger.info(
        "verification passed",
        extra={"workers": actual, "catalogs": len(wanted)},
    )

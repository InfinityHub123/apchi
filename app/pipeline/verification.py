"""Verification: did the Cluster adopt the configuration and stay healthy?

A different question from Validation, which asks whether the configuration should
be applied at all. Verification runs after Apply, against the real Cluster.

It is functional rather than introspective because Trino exposes no endpoint
reporting which configuration is live. Checking that the process restarted proves
nothing about whether it is running what was asked for.
"""

import asyncio
import logging
import time

from app.adapters.trino import SOURCE, Trino
from app.sections import SectionName
from app.sections.base import Cluster, Resources, SmokeQuery
from app.sections.registry import REGISTERED

logger = logging.getLogger(__name__)


class VerificationFailed(Exception):
    """The Cluster did not adopt the configuration, or is not healthy."""


#: How long the coordinator may take to start answering Apchi after a Rollout.
#:
#: Kubernetes calls a rollout complete the instant the new pod is ready, and the
#: Service follows a fraction of a second later: the old pod leaves the endpoint
#: list before the new one is programmed into it, so a request through the Service
#: in that window is refused. Measured on a one-replica Cluster, the gap straddles
#: the moment the Deployment reports complete -- the Deployment went to zero
#: unavailable replicas at 07:34:28.8 and the Service refused the request at
#: 07:34:29, recovering by 07:34:30. Verification's first request lands exactly
#: there, and without this it fails an Apply that worked and triggers an Auto
#: Rollback for a reason that would have cured itself.
#:
#: The Rollout has already proved Kubernetes considers the pod ready, so a
#: coordinator still silent after this long is a broken coordinator, which is what
#: Verification exists to catch.
_RESPONSE_GRACE_SECONDS = 30.0
_POLL_SECONDS = 1.0


async def _await_response(trino: Trino) -> None:
    """Returns once the coordinator answers and is past its own startup."""
    deadline = time.monotonic() + _RESPONSE_GRACE_SECONDS
    while True:
        starting = await trino.is_starting()
        if starting is False:
            return
        if time.monotonic() >= deadline:
            if starting is None:
                raise VerificationFailed(
                    f"The Trino coordinator did not respond within {_RESPONSE_GRACE_SECONDS:.0f}s."
                )
            raise VerificationFailed(
                f"The Trino coordinator was still starting {_RESPONSE_GRACE_SECONDS:.0f}s later."
            )
        await asyncio.sleep(_POLL_SECONDS)


async def verify(
    cluster: Cluster,
    desired: dict[SectionName, Resources],
    worker_deployment: str,
    verification_catalog: str,
) -> None:
    trino = cluster.trino

    # 1. The coordinator is responding and past its own startup.
    await _await_response(trino)

    # 2. Workers have registered. The readiness probe only proves the JVM booted --
    #    a coordinator with zero workers passes it. The expectation comes from
    #    Kubernetes so it adjusts when an Admin scales the Cluster, rather than
    #    drifting against a number configured in Apchi.
    expected = await cluster.kubernetes.ready_replicas(worker_deployment)
    actual = await trino.active_worker_count()
    if actual < expected:
        raise VerificationFailed(
            f"Only {actual} of {expected} workers have registered with the coordinator."
        )

    # 3. The Cluster can actually serve work. "Healthy" should not mean an endpoint
    #    returned 200. Run before the Sections rather than after, because what became of
    #    this query is evidence some of them need: Trino records the resource group a query
    #    ran in, and a Section proving its configuration is in force asks about a query that
    #    really ran rather than issuing one of its own.
    sql = f'SELECT 1 FROM "{verification_catalog}".runtime.nodes LIMIT 1'
    try:
        ran = await trino.run(sql)
    except Exception as exc:
        raise VerificationFailed(f"The smoke query failed: {exc}") from exc
    smoke = SmokeQuery(
        sql=sql, query_id=ran.query_id, user=cluster.settings.trino_user, source=SOURCE
    )

    # 4. Every Section confirms the Cluster adopted it. For catalogs this is what
    #    catches divergence between the Secret and Trino's store while the Apply is still
    #    in flight, rather than leaving it to surface at the next restart. Each Section
    #    knows what adoption means for itself; the pipeline only knows that a reason
    #    returned here fails the Apply.
    for section in REGISTERED:
        for reason in await section.verify(cluster, desired.get(section.name, {}), smoke):
            raise VerificationFailed(reason)

    logger.info("verification passed", extra={"workers": actual})

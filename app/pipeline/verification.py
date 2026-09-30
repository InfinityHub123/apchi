"""Verification: did the Cluster adopt the configuration and stay healthy?

A different question from Validation, which asks whether the configuration should
be applied at all. Verification runs after Apply, against the real Cluster.

It is functional rather than introspective because Trino exposes no endpoint
reporting which configuration is live. Checking that the process restarted proves
nothing about whether it is running what was asked for.
"""

import logging

from app.adapters.trino import SOURCE
from app.sections import SectionName
from app.sections.base import Cluster, Resources, SmokeQuery
from app.sections.registry import REGISTERED

logger = logging.getLogger(__name__)


class VerificationFailed(Exception):
    """The Cluster did not adopt the configuration, or is not healthy."""


async def verify(
    cluster: Cluster,
    desired: dict[SectionName, Resources],
    worker_deployment: str,
    verification_catalog: str,
) -> None:
    trino = cluster.trino

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

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
from typing import Final, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.trino import SOURCE, Trino
from app.sections import SectionName
from app.sections.base import Cluster, Resources, SmokeQuery
from app.sections.registry import REGISTERED

logger = logging.getLogger(__name__)

#: How one check came out. Three values, not two, and the third is the whole point --
#: Verification stops at the first failure, so the checks after it did not pass, did not
#: fail, and reporting them either way would be a lie about a Cluster nobody asked.
CheckOutcome = Literal["passed", "failed", "not_reached"]

COORDINATOR_RESPONDING: Final = "coordinator_responding"
WORKERS_REGISTERED: Final = "workers_registered"
SMOKE_QUERY: Final = "smoke_query"


def check_order() -> tuple[str, ...]:
    """Every check Verification makes, in the order it makes them.

    The per-Section entries come from the registry rather than a list kept here, so a
    Section added later appears without anybody remembering to add it -- which is the same
    reason the pipeline asks each Section what adoption means for itself.
    """
    return (
        COORDINATOR_RESPONDING,
        WORKERS_REGISTERED,
        SMOKE_QUERY,
        *(f"{section.name}_adopted" for section in REGISTERED),
    )


class CheckResult(BaseModel):
    """One check, and how far Verification got."""

    model_config = ConfigDict(extra="forbid")

    check: str = Field(description="Which check. `check_order()` lists them all, in order.")
    outcome: CheckOutcome
    detail: str | None = Field(
        default=None,
        description=(
            "Why it failed, or what it found when it passed -- '3 of 3 workers registered'. "
            "Null on a check that was never reached."
        ),
    )


class VerificationFailed(Exception):
    """The Cluster did not adopt the configuration, or is not healthy.

    Carries the checks as far as they got, because the failure is the only time anybody
    reads them and the reason is one of several different sentences.
    """

    def __init__(self, reason: str, checks: list[CheckResult] | None = None) -> None:
        super().__init__(reason)
        self.checks = checks or []


class _Checks:
    """The results so far, and an honest answer for the ones that never ran.

    Fail-fast is the right behaviour and not a limitation: there is no point asking a broken
    Cluster the next question, and Auto Rollback is waiting on this answer. What would be
    wrong is reporting the remaining checks as passed or as failed, so they are
    `not_reached`.
    """

    def __init__(self) -> None:
        self._order = check_order()
        self._results: dict[str, CheckResult] = {}

    def passed(self, check: str, detail: str | None = None) -> None:
        self._results[check] = CheckResult(check=check, outcome="passed", detail=detail)

    def failed(self, check: str, reason: str) -> NoReturn:
        """Record the failure, mark everything after it unreached, and stop."""
        self._results[check] = CheckResult(check=check, outcome="failed", detail=reason)
        raise VerificationFailed(reason, checks=self.results())

    def results(self) -> list[CheckResult]:
        return [
            self._results.get(check, CheckResult(check=check, outcome="not_reached"))
            for check in self._order
        ]


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
) -> list[CheckResult]:
    """Every check, or the first failure with the checks as far as they got.

    Returns the results rather than nothing, so an Apply can record *which* check decided
    its fate. "Verification failed" and "workers_registered failed, 1 of 3" are different
    amounts of help at three in the morning.
    """
    trino = cluster.trino
    checks = _Checks()

    # 1. The coordinator is responding and past its own startup.
    try:
        await _await_response(trino)
    except VerificationFailed as exc:
        checks.failed(COORDINATOR_RESPONDING, str(exc))
    checks.passed(COORDINATOR_RESPONDING)

    # 2. Workers have registered. The readiness probe only proves the JVM booted --
    #    a coordinator with zero workers passes it. The expectation comes from
    #    Kubernetes so it adjusts when an Admin scales the Cluster, rather than
    #    drifting against a number configured in Apchi.
    expected = await cluster.kubernetes.ready_replicas(worker_deployment)
    actual = await trino.active_worker_count()
    if actual < expected:
        checks.failed(
            WORKERS_REGISTERED,
            f"Only {actual} of {expected} workers have registered with the coordinator.",
        )
    checks.passed(WORKERS_REGISTERED, f"{actual} of {expected} workers registered.")

    # 3. The Cluster can actually serve work. "Healthy" should not mean an endpoint
    #    returned 200. Run before the Sections rather than after, because what became of
    #    this query is evidence some of them need: Trino records the resource group a query
    #    ran in, and a Section proving its configuration is in force asks about a query that
    #    really ran rather than issuing one of its own.
    sql = f'SELECT 1 FROM "{verification_catalog}".runtime.nodes LIMIT 1'
    try:
        ran = await trino.run(sql)
    except Exception as exc:
        checks.failed(SMOKE_QUERY, f"The smoke query failed: {exc}")
    smoke = SmokeQuery(
        sql=sql, query_id=ran.query_id, user=cluster.settings.trino_user, source=SOURCE
    )
    checks.passed(SMOKE_QUERY, f"query {ran.query_id} ran")

    # 4. Every Section confirms the Cluster adopted it. For catalogs this is what
    #    catches divergence between the Secret and Trino's store while the Apply is still
    #    in flight, rather than leaving it to surface at the next restart. Each Section
    #    knows what adoption means for itself; the pipeline only knows that a reason
    #    returned here fails the Apply.
    for section in REGISTERED:
        check = f"{section.name}_adopted"
        for reason in await section.verify(cluster, desired.get(section.name, {}), smoke):
            checks.failed(check, reason)
        checks.passed(check)

    logger.info("verification passed", extra={"workers": actual})
    return checks.results()

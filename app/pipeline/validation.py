"""Trino validation: proving a Candidate against a real coordinator before the
Cluster is touched.

Static validation already ran on every request, but static checks cannot tell you
whether Trino will accept a configuration. So Validation brings up an ephemeral
coordinator-only pod of the same image and version as the Cluster, issues the
Candidate's `CREATE CATALOG` statements against it, and throws it away.

This runs once per Apply against the whole Candidate, never once per resource: a pod
per request would be slow and expensive for something the Validate action already
offers on demand. A failure here leaves the Cluster completely unchanged, because
nothing has reached it yet. See section 6.
"""

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel

from app.adapters.kubernetes import KubernetesAdapter
from app.adapters.trino import Trino
from app.config import Settings
from app.sections import SectionName
from app.sections.catalogs.section import SECTION as CATALOGS

logger = logging.getLogger(__name__)

#: Every object Validation creates carries this, so orphans are findable. Apchi
#: sweeps them at startup: a crash mid-validation would otherwise leak a pod, and
#: leaked Trino pods are not cheap.
ROLE_LABEL = "apchi.dev/role"
VALIDATION_ROLE = "trino-validation"
VALIDATION_SELECTOR = f"{ROLE_LABEL}={VALIDATION_ROLE}"

#: The official image's own config reads `catalog.management` from this variable, so
#: dynamic catalogs need no config file of our own and no command override. The store
#: stays the default in-memory one: the probe is discarded, so there is nothing worth
#: persisting. The Cluster sets catalog.store=file because its catalogs must outlive
#: its pod; this one's must not.
_CATALOG_MANAGEMENT_ENV = "CATALOG_MANAGEMENT"

#: The image ships example catalogs -- jmx, memory, tpch, tpcds. An empty volume over
#: that directory hides them, so the probe holds exactly what the Candidate declares.
#: Otherwise a Catalog an Operator quite reasonably named `memory` would fail
#: Validation as already existing, for a reason having nothing to do with it.
_STATIC_CATALOG_DIR = "/etc/trino/catalog"

#: How often the probe is asked whether it is serving yet.
_POLL_SECONDS = 2.0


class ValidationFailure(BaseModel):
    """One reason a Candidate should not be applied, named well enough for an
    Operator to know what to fix."""

    section: SectionName | None = None
    resource: str | None = None
    reason: str

    def __str__(self) -> str:
        where = "/".join(part for part in (self.section, self.resource) if part)
        return f"{where}: {self.reason}" if where else self.reason


class ValidationFailed(Exception):
    """Raised with every failure found, not just the first.

    Collecting them is not the same as swallowing them: the operation still fails.
    An Operator fixing a Candidate wants the whole list, and each statement against
    the probe is independent of the others.
    """

    def __init__(self, failures: Sequence[ValidationFailure]) -> None:
        self.failures = list(failures)
        super().__init__("; ".join(str(failure) for failure in self.failures))


def pod_name(validation_id: str) -> str:
    """A DNS-1123 label carrying the id that caused it, so an orphan in `kubectl get
    pods` can be traced back to an Apply."""
    suffix = re.sub(r"[^a-z0-9]+", "-", validation_id.lower()).strip("-")
    return f"apchi-validate-{suffix}"[:63].rstrip("-")


def validation_manifest(name: str, image: str, deadline_seconds: int) -> dict[str, Any]:
    """A coordinator-only Trino and nothing else.

    No workers: this tests whether configuration *loads*, not whether queries run.
    No access control either -- the Cluster's rules are a Section of their own, and
    an unconfigured Trino allows the DDL this probe exists to issue.

    The image is left to start itself. Overriding the command to patch config in a
    shell was the first approach and a worse one: it duplicated a key the image
    already exposes as an environment variable, and it left the example catalogs in
    place.

    activeDeadlineSeconds is the backstop behind Apchi's own cleanup: an Apchi that
    dies mid-validation and never comes back still leaves a pod that stops itself.
    """
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "labels": {ROLE_LABEL: VALIDATION_ROLE, "app.kubernetes.io/managed-by": "apchi"},
        },
        "spec": {
            # A crash is a validation failure to report, never something to retry.
            "restartPolicy": "Never",
            "activeDeadlineSeconds": deadline_seconds,
            "containers": [
                {
                    "name": "trino",
                    "image": image,
                    "env": [{"name": _CATALOG_MANAGEMENT_ENV, "value": "dynamic"}],
                    "ports": [{"containerPort": 8080, "name": "http"}],
                    "volumeMounts": [{"name": "no-catalogs", "mountPath": _STATIC_CATALOG_DIR}],
                }
            ],
            "volumes": [{"name": "no-catalogs", "emptyDir": {}}],
        },
    }


@asynccontextmanager
async def ephemeral_trino(
    kubernetes: KubernetesAdapter, settings: Settings, validation_id: str
) -> AsyncIterator[Trino]:
    """An ephemeral coordinator, deleted however this block exits.

    Readiness is Trino's own `/v1/info` reporting `starting: false` -- the signal the
    standard readiness probe uses -- rather than the pod being Running, which happens
    well before Trino is serving.
    """
    image = await kubernetes.deployment_image(
        settings.coordinator_deployment_name, settings.trino_container_name
    )
    name = pod_name(validation_id)
    timeout = settings.validation_timeout_seconds

    await kubernetes.create_pod(validation_manifest(name, image, int(timeout) + 60))
    logger.info("validation pod created", extra={"pod": name, "image": image})
    try:
        yield await _await_serving(kubernetes, name, timeout)
    finally:
        await kubernetes.delete_pod(name)
        logger.info("validation pod deleted", extra={"pod": name})


async def _await_serving(kubernetes: KubernetesAdapter, name: str, timeout: float) -> Trino:
    deadline = time.monotonic() + timeout
    while True:
        state = await kubernetes.pod_state(name)
        if state.problem is not None:
            raise ValidationFailed(
                [
                    ValidationFailure(
                        reason=f"The validation coordinator could not start: {state.problem}"
                    )
                ]
            )
        if state.host is not None:
            probe = Trino(host=state.host, port=state.port)
            if await probe.is_starting() is False:
                logger.info("validation coordinator serving", extra={"pod": name})
                return probe
        if time.monotonic() >= deadline:
            raise ValidationFailed(
                [
                    ValidationFailure(
                        reason=(
                            f"The validation coordinator was not serving within {timeout:.0f}s "
                            f"(last seen {state.phase}). The Cluster was not touched."
                        )
                    )
                ]
            )
        await asyncio.sleep(_POLL_SECONDS)


def collision_failures(created: Sequence[str], live: set[str]) -> list[ValidationFailure]:
    """A Catalog the Candidate would create must not already exist on the Cluster.

    This is the whole-Candidate check that has teeth in slice 1. It catches a name
    that collides with a catalog nobody brought under management -- one seeded before
    Apchi, say. The ephemeral probe cannot catch it, because the probe starts empty:
    the collision exists only on the Cluster.

    Without the check the DDL fails *after* Apply has written the Secret, which is
    the divergence of section 10 for a reason an Operator could have been told about
    before anything moved.
    """
    return [
        ValidationFailure(
            section=CATALOGS,
            resource=name,
            reason=(
                f"A catalog named {name!r} already exists on the Cluster and is not "
                "managed by Apchi. Adopt it or choose another name."
            ),
        )
        for name in sorted(set(created) & live)
    ]


async def _catalog_failures(probe: Trino, catalogs: dict[str, Any]) -> list[ValidationFailure]:
    from trino.exceptions import TrinoQueryError

    failures: list[ValidationFailure] = []
    for name in sorted(catalogs):
        stored = catalogs[name]
        try:
            await probe.create_catalog(name, stored["connector"], stored.get("properties", {}))
        except TrinoQueryError as exc:
            failures.append(
                ValidationFailure(section=CATALOGS, resource=name, reason=str(exc.message))
            )
        except ValueError as exc:
            # A connector name Apchi will not put in a statement at all.
            failures.append(ValidationFailure(section=CATALOGS, resource=name, reason=str(exc)))
    return failures


async def validate_candidate(
    sections: dict[SectionName, dict[str, Any]],
    created: Sequence[str],
    cluster: Trino,
    kubernetes: KubernetesAdapter,
    settings: Settings,
    validation_id: str,
) -> None:
    """Raises ValidationFailed if the Candidate should not be applied.

    Checks that need no pod run first, so a Candidate that cannot possibly work is
    rejected without paying for a coordinator.

    References *between* Sections are checked here too, over the whole Candidate,
    because request-time validation deliberately stops at the resource in front of it
    -- adding a permission before the catalog it names has to be allowed, since the
    Candidate is coherent once both exist. Slice 1 has one Section and a Catalog
    references no other Apchi resource, so the first such reference arrives with
    Client Certificates.
    """
    if created and (failures := collision_failures(created, await cluster.catalogs())):
        raise ValidationFailed(failures)

    catalogs = sections.get(CATALOGS, {})
    if not catalogs:
        # No pod for a Candidate with nothing a coordinator could reject. The pod is
        # the expensive part of Validation, and an empty Candidate is a real case:
        # the first Apply of a fresh Cluster, and every Apply that only drops things.
        logger.info("no Trino validation needed")
        return

    async with ephemeral_trino(kubernetes, settings, validation_id) as probe:
        failures = await _catalog_failures(probe, catalogs)

    if failures:
        raise ValidationFailed(failures)
    logger.info("validation passed", extra={"catalogs": len(catalogs)})

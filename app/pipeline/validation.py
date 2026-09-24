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
from pathlib import Path
from typing import Any

from app.adapters.kubernetes import KubernetesAdapter
from app.adapters.trino import Trino
from app.config import Settings
from app.sections import SectionName
from app.sections.base import Cluster, Resources, SectionPlan, ValidationFailure
from app.sections.registry import REGISTERED

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

#: The volume carrying whatever files the Sections need inside the probe.
_FILES_VOLUME = "section-files"

#: How much of a failed probe's log to quote back. Enough to carry Trino's error, little
#: enough not to put a wall of startup output in an API response.
_LOG_LINES = 40

LABELS = {ROLE_LABEL: VALIDATION_ROLE, "app.kubernetes.io/managed-by": "apchi"}


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


def validation_manifest(
    name: str, image: str, deadline_seconds: int, files: dict[str, str] | None = None
) -> dict[str, Any]:
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
            "labels": dict(LABELS),
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
                    "volumeMounts": [
                        {"name": "no-catalogs", "mountPath": _STATIC_CATALOG_DIR},
                        *_file_mounts(files or {}),
                    ],
                }
            ],
            "volumes": [
                {"name": "no-catalogs", "emptyDir": {}},
                *([{"name": _FILES_VOLUME, "secret": {"secretName": name}}] if files else []),
            ],
        },
    }


def _file_mounts(files: dict[str, str]) -> list[dict[str, Any]]:
    """One subPath mount per file a Section needs in the probe.

    subPath is right here for the reason it is wrong on the Cluster: the probe is a fresh
    pod every time, so a mount that never updates is a mount that never needs to.
    """
    return [
        {"name": _FILES_VOLUME, "mountPath": path, "subPath": Path(path).name, "readOnly": True}
        for path in sorted(files)
    ]


def probe_secret(files: dict[str, str]) -> dict[str, str]:
    """The probe's companion Secret, keyed by filename.

    Two Sections wanting files of the same name would collide, so it is asserted rather
    than assumed: the Secret is flat and the mount paths are not.
    """
    keyed = {Path(path).name: content for path, content in files.items()}
    if len(keyed) != len(files):
        raise ValueError(f"two Sections want probe files with the same name: {sorted(files)}")
    return keyed


@asynccontextmanager
async def ephemeral_trino(
    kubernetes: KubernetesAdapter,
    settings: Settings,
    validation_id: str,
    files: dict[str, str] | None = None,
    resource: str | None = None,
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

    if files:
        # Born and dies with the pod, and labelled like it, so a crash mid-Validation
        # leaves a Secret the startup sweep can find.
        await kubernetes.create_secret(name, probe_secret(files), dict(LABELS))
    await kubernetes.create_pod(validation_manifest(name, image, int(timeout) + 60, files))
    logger.info(
        "validation pod created", extra={"pod": name, "image": image, "files": len(files or {})}
    )
    try:
        yield await _await_serving(kubernetes, name, timeout, resource)
    finally:
        await kubernetes.delete_pod(name)
        if files:
            await kubernetes.delete_secret(name)
        logger.info("validation pod deleted", extra={"pod": name})


def _why(logs: str) -> str:
    """The lines of a failed probe's log that say what went wrong."""
    interesting = [
        line.strip()
        for line in logs.splitlines()
        if "ERROR" in line or "Caused by" in line or "Configuration is invalid" in line
    ]
    return " / ".join(interesting[-3:])


async def _await_serving(
    kubernetes: KubernetesAdapter, name: str, timeout: float, resource: str | None = None
) -> Trino:
    deadline = time.monotonic() + timeout
    while True:
        state = await kubernetes.pod_state(name)
        if state.problem is not None:
            # A probe that will not start is how a file-based Section fails Validation, and
            # its log is the only place the reason exists: Trino writes nothing to the
            # termination-log file Kubernetes would otherwise surface.
            raise ValidationFailed(
                [
                    ValidationFailure(
                        resource=resource,
                        reason=(
                            f"The validation coordinator could not start: {state.problem}. "
                            f"{_why(await kubernetes.pod_logs(name, _LOG_LINES))}"
                        ).strip(),
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


async def validate_candidate(
    sections: dict[SectionName, Resources],
    plans: dict[SectionName, SectionPlan],
    cluster: Cluster,
    validation_id: str,
) -> None:
    """Raises ValidationFailed if the Candidate should not be applied.

    Two phases, because one of them is expensive. Every Section's own checks run first,
    against the Cluster and the Candidate alone; only if some Section has something an
    ephemeral coordinator could reject is a pod created at all.

    References *between* Sections are checked here too, over the whole Candidate,
    because request-time validation deliberately stops at the resource in front of it --
    adding a permission before the catalog it names has to be allowed, since the
    Candidate is coherent once both exist. With one Section registered and no Section
    referencing another there is nothing of that kind to check yet.
    """
    failures: list[ValidationFailure] = []
    for section in REGISTERED:
        desired = sections.get(section.name, {})
        failures.extend(await section.check(cluster, desired, plans[section.name]))
    if failures:
        raise ValidationFailed(failures)

    probing = [
        section for section in REGISTERED if section.needs_probe(sections.get(section.name, {}))
    ]
    if not probing:
        # No pod for a Candidate with nothing a coordinator could reject. The pod is the
        # expensive part of Validation, and an empty Candidate is a real case: the first
        # Apply of a fresh Cluster, and every Apply that only drops things.
        logger.info("no Trino validation needed")
        return

    files: dict[str, str] = {}
    for section in probing:
        files.update(section.probe_files(sections.get(section.name, {})))

    # Named so that a probe which refuses to start can be blamed on something. With one
    # file-based Section that is unambiguous; a second will need the attribution to come
    # from whichever Section contributed the file Trino choked on.
    culprit = next(
        (
            sorted(sections.get(section.name, {}))[0]
            for section in probing
            if section.probe_files(sections.get(section.name, {}))
        ),
        None,
    )

    async with ephemeral_trino(
        cluster.kubernetes, cluster.settings, validation_id, files, culprit
    ) as probe:
        for section in probing:
            failures.extend(
                await section.check_against_probe(probe, sections.get(section.name, {}))
            )

    if failures:
        raise ValidationFailed(failures)
    logger.info("validation passed", extra={"sections": len(probing)})

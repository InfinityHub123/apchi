"""What each Trino Deployment was started from, read off the Deployment itself (#290).

A Secret says what Trino runs after its next restart, not what it runs now: these files are
read once at start, and a subPath mount never updates inside a running pod. So each restart
writes a checksum of them onto the pod template, and a pod carries the checksum of the files
it started from. Nothing about what a Cluster runs is kept in MongoDB: a restored backup or a
disaster-recovery copy cannot then disagree with the Cluster in front of Apchi.

Only the restart-requiring files go in. The rules and the catalogs change without a restart,
so a checksum of them would restart Trino for a change it had already adopted.
"""

import hashlib
import json
import logging
from typing import Final

from app.adapters.kubernetes import PREFIX
from app.pipeline import raw_files
from app.pipeline.files import by_secret
from app.pipeline.server_configuration import ROLES
from app.sections import SectionName
from app.sections.base import Cluster, Resources
from app.sections.registry import REGISTERED
from app.sections.server_configuration import Role, role_secret, secret_name

logger = logging.getLogger(__name__)

#: On the Deployment itself, not its pod template, so writing it restarts nothing: the
#: Snapshot the Cluster was last verified on, for whoever reads `kubectl describe`. Apchi
#: never decides anything from it.
VERIFIED_SNAPSHOT: Final = f"{PREFIX}verified-snapshot"


def startup_files(
    role: Role, cluster: Cluster, sections: dict[SectionName, Resources]
) -> dict[str, dict[str, str]]:
    """Every Secret a role's pods read only at start, as Apchi would render it now.

    Rendered rather than read back, so the answer depends only on the Snapshot, the Admin
    values and the chart's wiring -- the three things that can move a restart.
    """
    settings, admin = cluster.settings, cluster.admin
    files = {secret_name(role, settings): role_secret(role, admin.server_configuration, settings)}
    if role == "worker":
        return files
    for section in REGISTERED:
        declared = section.coordinator_files(settings)
        if section.requires_rollout and declared:
            rendered = section.render_files(sections.get(section.name, {}), settings, admin)
            files.update(by_secret(declared, rendered))
    # Keyed by a digest of the path, so a raw file moved to another path moves the checksum
    # as surely as one whose content changed.
    files[settings.raw_files_secret_name] = {
        raw_files.secret_key(path): content for path, content in raw_files.planned(admin).items()
    }
    return files


def checksum(role: Role, cluster: Cluster, sections: dict[SectionName, Resources]) -> str:
    encoded = json.dumps(startup_files(role, cluster, sections), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def deployment(role: Role, cluster: Cluster) -> str:
    settings = cluster.settings
    return (
        settings.coordinator_deployment_name
        if role == "coordinator"
        else settings.worker_deployment_name
    )


async def behind(cluster: Cluster, sections: dict[SectionName, Resources]) -> set[Role]:
    """The roles whose pods were started from other files than these.

    A Deployment with no checksum says nothing either way -- Apchi never restarted it, as on
    a fresh install, whose pods started after Apchi wrote their Secrets -- and the caller's
    comparison of the Secrets decides alone.
    """
    stale: set[Role] = set()
    for role in ROLES:
        live = await cluster.kubernetes.rolled_out_checksum(deployment(role, cluster))
        if live is not None and live != checksum(role, cluster, sections):
            stale.add(role)
    if stale:
        logger.warning(
            "trino was started from other files than these",
            extra={"roles": sorted(stale)},
        )
    return stale


async def record_verified(cluster: Cluster, snapshot: int | None) -> None:
    """Say on each Deployment which Snapshot the Cluster was verified on.

    For people only, so a failure is logged and never fails an Apply that has already
    committed: the Cluster is right, only its description is missing.
    """
    if snapshot is None:
        return
    for role in ROLES:
        name = deployment(role, cluster)
        try:
            await cluster.kubernetes.annotate_deployment(name, {VERIFIED_SNAPSHOT: str(snapshot)})
        except Exception:
            logger.error(
                "could not record the verified Snapshot on %s",
                name,
                exc_info=True,
                extra={"snapshot": snapshot},
            )

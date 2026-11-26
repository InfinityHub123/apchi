"""Writing the Secrets Apchi owns when they do not exist -- at startup, and never otherwise.

Apchi writes into nine Secrets: the catalog seed, one per Section that renders a file, the
server configuration of each role, and the raw files. None of them is the chart's to create
(ADR-0010). The chart used to create the Sections' and the seed as Helm `pre-install` hooks,
which Helm runs once -- but Argo CD does not run Helm. It turns those hooks into sync hooks and
runs them on every sync, deleting and recreating each Secret with the chart's starting contents:
measured on kind, one sync for a pod annotation emptied the catalog seed, reset the
access-control rules to fail-open, and left the coordinator crash-looping on emptied Secrets its
mounts still read from.

So Apchi creates them itself. What goes into one is what the Cluster runs: the latest Snapshot,
rendered with the Admin values in force -- which on a new install is the empty configuration,
the same starting point the chart used to ship. A Secret that exists is never rewritten here:
its content is what the running pods were started with, and restarting Apchi must never change
the Cluster. Rewriting belongs to an Apply.

Trino cannot start without some of these -- it reads the access-control rules and the
user-mapping file while loading -- so its pods wait in ContainerCreating until they appear,
which Kubernetes retries by itself (measured).
"""

import logging
from typing import Any

from app.adapters.kubernetes import KubernetesAdapter
from app.config import Settings
from app.pipeline.admin_values import AdminStore
from app.pipeline.files import by_secret
from app.pipeline.raw_files import secret_key as raw_file_key
from app.pipeline.snapshots import SnapshotStore
from app.sections import SectionName
from app.sections.admin import AdminValues
from app.sections.catalogs import SECTION as CATALOGS
from app.sections.catalogs.generator import render_secret
from app.sections.registry import REGISTERED
from app.sections.server_configuration import role_secret, secret_name

logger = logging.getLogger(__name__)


def desired_secrets(
    settings: Settings, sections: dict[SectionName, dict[str, Any]], admin: AdminValues
) -> dict[str, dict[str, str]]:
    """Every Secret Apchi owns, as it would write each for this configuration."""
    secrets: dict[str, dict[str, str]] = {
        settings.catalog_secret_name: render_secret({CATALOGS: sections.get(CATALOGS, {})}),
        settings.raw_files_secret_name: {
            raw_file_key(path): content for path, content in admin.raw_files.items()
        },
    }
    for section in REGISTERED:
        declared = section.coordinator_files(settings)
        if not declared:
            continue
        contents = section.render_files(sections.get(section.name, {}), settings, admin)
        secrets.update(by_secret(declared, contents))
    for role in ("coordinator", "worker"):
        secrets[secret_name(role, settings)] = role_secret(
            role, admin.server_configuration, settings
        )
    return secrets


async def write_missing(
    kubernetes: KubernetesAdapter,
    settings: Settings,
    snapshots: SnapshotStore,
    admin: AdminStore,
) -> list[str]:
    """Write each Secret Apchi owns that does not exist. Returns the names written."""
    latest = await snapshots.latest()
    sections = await snapshots.sections_of(None if latest is None else latest.number)
    values = await admin.load()
    written: list[str] = []
    for name, data in desired_secrets(settings, sections, values).items():
        if await kubernetes.read_secret_if_present(name) is not None:
            continue
        await kubernetes.write_secret(name, data)
        written.append(name)
    if written:
        logger.warning(
            "wrote the Secrets Apchi owns that did not exist",
            extra={"secrets": written, "snapshot": None if latest is None else latest.number},
        )
    return written

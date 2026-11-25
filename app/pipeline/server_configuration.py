"""Delivering Trino's own server configuration (§14, ADR-0010).

Apchi writes one Secret per role and the chart mounts each key of it with subPath. Apchi never
touches that mount -- it is fixed in the chart, which leaves Argo CD nothing to reconcile --
so delivering is writing the Secret, and the Rollout that follows is what makes the content
live. A rewritten Secret does nothing to a running pod: measured on kind, the pod kept the
file it started with until it restarted.
"""

import logging
from typing import TYPE_CHECKING

from app.adapters.kubernetes import KubernetesAdapter
from app.config import Settings
from app.sections.admin import AdminValues
from app.sections.server_configuration import coordinator_secret

if TYPE_CHECKING:  # a Cluster carries the adapters and the Admin values together
    from app.sections.base import Cluster

logger = logging.getLogger(__name__)


async def deliver(cluster: "Cluster") -> None:
    """Write the coordinator's server configuration as the Admin values now render it."""
    await cluster.kubernetes.write_secret(
        cluster.settings.server_configuration_secret_name,
        coordinator_secret(cluster.admin.server_configuration, cluster.settings),
    )
    logger.info("delivered the coordinator's server configuration")


async def changed(cluster: "Cluster") -> bool:
    """Whether delivering would change what the coordinator was started with.

    A diff of two durable records, the rendered Secret and the live one, for the reason §11
    settles rollback that way: it needs no history of its own, and it also notices wiring the
    chart changed and a Secret somebody edited by hand. Every one of these files is read once
    at start, so any difference means a Rollout.
    """
    live = await cluster.kubernetes.read_secret_if_present(
        cluster.settings.server_configuration_secret_name
    )
    return live != coordinator_secret(cluster.admin.server_configuration, cluster.settings)


async def write_if_missing(
    kubernetes: KubernetesAdapter, settings: Settings, admin: AdminValues
) -> bool:
    """At startup: write the Secret only if it does not exist. True when it was written.

    Trino cannot start without its files, and on a new install nothing has written them yet --
    the coordinator waits in ContainerCreating until the Secret appears, which Kubernetes
    retries by itself (measured). An existing Secret is never rewritten here, even when the
    wiring has moved: what is in it is what the running coordinator was started with, and
    restarting Apchi must never end a query. The next Apply delivers the difference, and
    Review shows it as a restart before then.
    """
    name = settings.server_configuration_secret_name
    if await kubernetes.read_secret_if_present(name) is not None:
        return False
    await kubernetes.write_secret(name, coordinator_secret(admin.server_configuration, settings))
    logger.warning(
        "wrote the coordinator's server configuration, which did not exist",
        extra={"secret": name},
    )
    return True

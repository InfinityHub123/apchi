"""Delivering Trino's own server configuration (§14, ADR-0010).

Apchi writes one Secret per role and the chart mounts each key of it with subPath. Apchi never
touches those mounts -- they are fixed in the chart, which leaves Argo CD nothing to reconcile --
so delivering is writing the Secrets, and the Rollout that follows is what makes the content
live. A rewritten Secret does nothing to a running pod: measured on kind, the pod kept the file
it started with until it restarted.

The roles are restarted separately because they cost differently. The coordinator cannot drain,
so restarting it ends every running query (ADR-0003); a worker drains its tasks first, so a
worker-only change costs no query at all. A change to one role's files restarts that role only.
"""

import logging
from typing import TYPE_CHECKING, Final

from app.adapters.kubernetes import KubernetesAdapter
from app.config import Settings
from app.sections.admin import AdminValues
from app.sections.server_configuration import Role, role_secret, secret_name

if TYPE_CHECKING:  # a Cluster carries the adapters and the Admin values together
    from app.sections.base import Cluster

logger = logging.getLogger(__name__)

ROLES: Final[tuple[Role, ...]] = ("coordinator", "worker")


async def deliver(cluster: "Cluster") -> None:
    """Write every role's server configuration as the Admin values now render it."""
    for role in ROLES:
        await cluster.kubernetes.write_secret(
            secret_name(role, cluster.settings),
            role_secret(role, cluster.admin.server_configuration, cluster.settings),
        )
    logger.info("delivered the server configuration")


async def changed_roles(cluster: "Cluster") -> set[Role]:
    """Which roles' files delivering would change from what their pods were started with.

    A diff of two durable records, the rendered Secret and the live one, for the reason §11
    settles rollback that way: it needs no history of its own, and it also notices wiring the
    chart changed and a Secret somebody edited by hand. Every one of these files is read once at
    start, so a role whose files differ is a role to restart.
    """
    moved: set[Role] = set()
    for role in ROLES:
        live = await cluster.kubernetes.read_secret_if_present(secret_name(role, cluster.settings))
        if live != role_secret(role, cluster.admin.server_configuration, cluster.settings):
            moved.add(role)
    return moved


async def changed(cluster: "Cluster") -> bool:
    """Whether delivering would change any role's files."""
    return bool(await changed_roles(cluster))


async def write_if_missing(
    kubernetes: KubernetesAdapter, settings: Settings, admin: AdminValues
) -> list[Role]:
    """At startup: write each role's Secret only if it does not exist. Returns the roles written.

    Trino cannot start without its files, and on a new install nothing has written them yet --
    the pods wait in ContainerCreating until the Secret appears, which Kubernetes retries by
    itself (measured). An existing Secret is never rewritten here, even when the wiring has
    moved: what is in it is what the running pods were started with, and restarting Apchi must
    never end a query. The next Apply delivers the difference, and Review shows it before then.
    """
    written: list[Role] = []
    for role in ROLES:
        name = secret_name(role, settings)
        if await kubernetes.read_secret_if_present(name) is not None:
            continue
        await kubernetes.write_secret(name, role_secret(role, admin.server_configuration, settings))
        logger.warning(
            "wrote the %s's server configuration, which did not exist",
            role,
            extra={"secret": name},
        )
        written.append(role)
    return written

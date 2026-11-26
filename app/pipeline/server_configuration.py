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
    await take_off_the_old_listener(cluster)
    logger.info("delivered the server configuration")


async def take_off_the_old_listener(cluster: "Cluster") -> bool:
    """Remove the mount the Event Listeners Section put on the coordinator, and its Secret.

    The last time Apchi edits the coordinator's pod template for a listener (#269). Harmless
    left in place -- with `event-listener.config-files` set, Trino ignores its default path
    (measured on 483) -- but it is a volume the chart does not render, which Argo CD shows as
    drift, over a Secret holding the listener's credentials that nothing writes any more.
    Here rather than at startup, because restarting Apchi must never change the Cluster: an
    Admin Apply is about to restart the coordinator anyway.
    """
    settings = cluster.settings
    spec = await cluster.kubernetes.deployment_pod_spec(settings.coordinator_deployment_name)
    volume = settings.event_listener_volume_name
    if not any(v.get("name") == volume for v in spec.get("volumes", [])):
        return False
    paths = [
        mount["mountPath"]
        for container in spec.get("containers", [])
        if container.get("name") == settings.trino_container_name
        for mount in container.get("volumeMounts", [])
        if mount.get("name") == volume
    ]
    await cluster.kubernetes.unmount_secret_files(
        settings.coordinator_deployment_name,
        settings.trino_container_name,
        volume,
        paths,
        drop_volume=True,
    )
    await cluster.kubernetes.delete_secret(settings.event_listener_secret_name)
    logger.warning(
        "took the retired Event Listener mount off the coordinator",
        extra={"volume": volume, "secret": settings.event_listener_secret_name},
    )
    return True


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

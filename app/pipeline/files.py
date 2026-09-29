"""Delivering the files Sections declare.

One place does the writing, the mounting and the unmounting, so a Section that owns a file
says where it goes and nothing more. Before this, delivering one took the Secret name, the
volume name, the path and the key spelled out at every call site and imported by name into
the precondition checks -- which is three copies for three Sections, and a precondition
that grows a branch each time, the branch being the thing nobody notices is missing.
"""

import logging

from app.config import Settings
from app.sections.base import Cluster, Resources, Section

logger = logging.getLogger(__name__)


async def deliver(section: Section, cluster: Cluster, desired: Resources) -> None:
    """Write this Section's file and make sure it is mounted -- or unmounted.

    Nothing is adopted here. A Section whose file only takes effect on a restart says so
    with `requires_rollout`, and the pipeline runs the Rollout.
    """
    spec = section.coordinator_file(cluster.settings)
    if spec is None:
        return

    content = section.render_file(desired)
    await cluster.kubernetes.write_secret(spec.secret, {spec.key: content} if content else {})

    if content:
        await cluster.kubernetes.mount_secret_file(
            cluster.settings.coordinator_deployment_name,
            cluster.settings.trino_container_name,
            spec.volume,
            spec.secret,
            spec.path,
            spec.key,
        )
    else:
        await cluster.kubernetes.unmount_secret_file(
            cluster.settings.coordinator_deployment_name,
            cluster.settings.trino_container_name,
            spec.volume,
            spec.path,
        )
    logger.info("delivered %s", spec.path, extra={"section": section.name})


async def would_change(section: Section, cluster: Cluster, resources: Resources) -> bool:
    """Whether delivering `resources` would change what the Cluster holds.

    Auto Rollback asks this to decide whether undoing a Section needs a restart, and it
    cannot be answered from the failed Apply's plan: recovery after an Apchi restart has no
    plan, because the process that made it is gone.
    """
    spec = section.coordinator_file(cluster.settings)
    if spec is None:
        return False
    content = section.render_file(resources)
    current = await cluster.kubernetes.read_secret(spec.secret)
    return current != ({spec.key: content} if content else {})


def probe_files(section: Section, settings: Settings, desired: Resources) -> dict[str, str]:
    """What the validation probe must hold for this Section.

    Derived from the same declaration rather than answered separately: a Section's file is
    a file wherever it is, and starting the probe with it in place is how a file Trino will
    not accept becomes a pod that will not start rather than a Cluster that will not.
    """
    spec = section.coordinator_file(settings)
    content = section.render_file(desired)
    if spec is None or not content:
        return {}
    return {spec.path: content}


def owned_paths(sections: tuple[Section, ...], settings: Settings) -> dict[str, str]:
    """Every path Apchi mounts something at, to the volume it uses there.

    The preconditions read this rather than naming a Section, so a new file-owning Section
    is guarded without anyone remembering to add it.
    """
    paths: dict[str, str] = {}
    for section in sections:
        spec = section.coordinator_file(settings)
        if spec is not None:
            paths[spec.path] = spec.volume
    return paths

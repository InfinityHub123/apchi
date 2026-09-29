"""Delivering the files Sections declare.

One place does the writing, the mounting and the unmounting, so a Section that owns a file
says where it goes and nothing more. Before this, delivering one took the Secret name, the
volume name, the path and the key spelled out at every call site and imported by name into
the precondition checks -- which is three copies for three Sections, and a precondition
that grows a branch each time, the branch being the thing nobody notices is missing.
"""

import logging

from app.config import Settings
from app.sections.admin import AdminValues
from app.sections.base import Cluster, CoordinatorFile, Resources, Section

logger = logging.getLogger(__name__)


async def deliver(section: Section, cluster: Cluster, desired: Resources) -> None:
    """Write this Section's files and make sure each is mounted -- or unmounted.

    Nothing is adopted here. A Section whose files only take effect on a restart says so
    with `requires_rollout`, and the pipeline runs the Rollout.
    """
    specs = section.coordinator_files(cluster.settings)
    if not specs:
        return

    contents = section.render_files(desired, cluster.settings, cluster.admin)
    for secret, data in _by_secret(specs, contents).items():
        await cluster.kubernetes.write_secret(secret, data)

    # Grouped by volume, because a volume and the mounts that name it can only be removed
    # together: the API server rejects a volume removed while a mount still names it, and a
    # volume left behind with no mounts is dead weight on the Admin's pod template.
    for volume, group in _by_volume(specs).items():
        present = [spec for spec in group if contents.get(spec.path)]
        absent = [spec for spec in group if not contents.get(spec.path)]
        for spec in present:
            await cluster.kubernetes.mount_secret_file(
                cluster.settings.coordinator_deployment_name,
                cluster.settings.trino_container_name,
                volume,
                spec.secret,
                spec.path,
                spec.key,
            )
        if absent:
            await cluster.kubernetes.unmount_secret_files(
                cluster.settings.coordinator_deployment_name,
                cluster.settings.trino_container_name,
                volume,
                [spec.path for spec in absent],
                drop_volume=not present,
            )
        for spec in group:
            logger.info("delivered %s", spec.path, extra={"section": section.name})


def _by_volume(specs: tuple[CoordinatorFile, ...]) -> dict[str, list[CoordinatorFile]]:
    volumes: dict[str, list[CoordinatorFile]] = {}
    for spec in specs:
        volumes.setdefault(spec.volume, []).append(spec)
    return volumes


def _by_secret(
    specs: tuple[CoordinatorFile, ...], contents: dict[str, str]
) -> dict[str, dict[str, str]]:
    """One write per Secret, however many files a Section keeps in it.

    Written whole rather than key by key: a Secret Apchi owns holds exactly what the Section
    renders, so a file that stopped being rendered stops being in the Secret.
    """
    secrets: dict[str, dict[str, str]] = {spec.secret: {} for spec in specs}
    for spec in specs:
        if content := contents.get(spec.path):
            secrets[spec.secret][spec.key] = content
    return secrets


async def would_change(section: Section, cluster: Cluster, resources: Resources) -> bool:
    """Whether delivering `resources` would change what the Cluster holds.

    Auto Rollback asks this to decide whether undoing a Section needs a restart, and Review
    asks it to decide whether an Apply restarts at all -- neither can be answered from a
    plan: an Admin change moves no Section, and recovery after an Apchi restart has no plan
    because the process that made it is gone.
    """
    specs = section.coordinator_files(cluster.settings)
    if not specs:
        return False
    contents = section.render_files(resources, cluster.settings, cluster.admin)
    for secret, data in _by_secret(specs, contents).items():
        if await cluster.kubernetes.read_secret(secret) != data:
            return True
    return False


def probe_files(
    section: Section, settings: Settings, desired: Resources, admin: AdminValues
) -> dict[str, str]:
    """What the validation probe must hold for this Section.

    Derived from the same declaration rather than answered separately: a Section's file is
    a file wherever it is, and starting the probe with it in place is how a file Trino will
    not accept becomes a pod that will not start rather than a Cluster that will not.
    """
    declared = {spec.path for spec in section.coordinator_files(settings)}
    contents = section.render_files(desired, settings, admin)
    return {path: content for path, content in contents.items() if path in declared and content}


def owned_paths(sections: tuple[Section, ...], settings: Settings) -> dict[str, str]:
    """Every path Apchi mounts something at, to the volume it uses there.

    The preconditions read this rather than naming a Section, so a new file-owning Section
    is guarded without anyone remembering to add it.
    """
    return {
        spec.path: spec.volume
        for section in sections
        for spec in section.coordinator_files(settings)
    }

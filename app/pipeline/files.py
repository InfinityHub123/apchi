"""Delivering the files Sections declare.

One place does the writing, the mounting and the unmounting, so a Section that owns a file
says where it goes and nothing more. Before this, delivering one took the Secret name, the
volume name, the path and the key spelled out at every call site and imported by name into
the precondition checks -- which is three copies for three Sections, and a precondition
that grows a branch each time, the branch being the thing nobody notices is missing.
"""

import logging
from pathlib import PurePosixPath

from app.config import Settings
from app.sections.admin import AdminValues
from app.sections.base import (
    Cluster,
    CoordinatorDirectory,
    CoordinatorFile,
    Delivery,
    Resources,
    Section,
)

logger = logging.getLogger(__name__)


async def deliver(section: Section, cluster: Cluster, desired: Resources) -> None:
    """Write this Section's files and make sure each is mounted -- or unmounted.

    Nothing is adopted here. A Section whose files only take effect on a restart says so
    with `requires_rollout`, and the pipeline runs the Rollout.
    """
    declared = section.coordinator_files(cluster.settings)
    if not declared:
        return

    contents = section.render_files(desired, cluster.settings, cluster.admin)
    for secret, data in _by_secret(declared, contents).items():
        await cluster.kubernetes.write_secret(secret, data)

    # Grouped by volume, because a volume and the mounts that name it can only be removed
    # together: the API server rejects a volume removed while a mount still names it, and a
    # volume left behind with no mounts is dead weight on the Admin's pod template.
    # Directories are not in this at all -- writing the Secret is the whole of Apchi's part.
    for volume, group in _by_volume(declared).items():
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
    for path in sorted(contents):
        logger.info("delivered %s", path, extra={"section": section.name})


def _by_volume(declared: tuple[Delivery, ...]) -> dict[str, list[CoordinatorFile]]:
    """Only the files Apchi mounts. A directory arrives by being in a Secret the Admin
    already mounts, so there is no mount for Apchi to add and none to take away."""
    volumes: dict[str, list[CoordinatorFile]] = {}
    for spec in declared:
        if isinstance(spec, CoordinatorFile):
            volumes.setdefault(spec.volume, []).append(spec)
    return volumes


def _by_secret(
    declared: tuple[Delivery, ...], contents: dict[str, str]
) -> dict[str, dict[str, str]]:
    """One write per Secret, holding exactly what the Section rendered into it.

    Written whole rather than key by key, so a file that stopped being rendered -- a removed
    certificate, a Section emptied -- stops being in the Secret rather than lingering in the
    durable copy and reappearing at the next pod start.
    """
    secrets: dict[str, dict[str, str]] = {spec.secret: {} for spec in declared}
    unclaimed = dict(contents)
    for spec in declared:
        if isinstance(spec, CoordinatorFile):
            if content := unclaimed.pop(spec.path, None):
                secrets[spec.secret][spec.key] = content
        else:
            for path in [
                p for p in unclaimed if PurePosixPath(p).parent == PurePosixPath(spec.path)
            ]:
                if content := unclaimed.pop(path):
                    secrets[spec.secret][PurePosixPath(path).name] = content
    if unclaimed:
        raise ValueError(
            f"{section_of(declared)} rendered files nowhere declared: {sorted(unclaimed)}"
        )
    return secrets


def section_of(declared: tuple[Delivery, ...]) -> str:
    """Only for the error above: which Secrets were on offer when a path matched none."""
    return f"a Section writing {sorted({spec.secret for spec in declared})}"


async def would_change(section: Section, cluster: Cluster, resources: Resources) -> bool:
    """Whether delivering `resources` would change what the Cluster holds.

    Auto Rollback asks this to decide whether undoing a Section needs a restart, and Review
    asks it to decide whether an Apply restarts at all -- neither can be answered from a
    plan: an Admin change moves no Section, and recovery after an Apchi restart has no plan
    because the process that made it is gone.
    """
    declared = section.coordinator_files(cluster.settings)
    if not declared:
        return False
    contents = section.render_files(resources, cluster.settings, cluster.admin)
    for secret, data in _by_secret(declared, contents).items():
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
    if not section.coordinator_files(settings):
        return {}
    return {
        path: content
        for path, content in section.render_files(desired, settings, admin).items()
        if content
    }


def owned_paths(sections: tuple[Section, ...], settings: Settings) -> dict[str, str]:
    """Every path Apchi mounts something at, to the volume it uses there.

    The preconditions read this rather than naming a Section, so a new file-owning Section
    is guarded without anyone remembering to add it. A file the Admin mounts is deliberately
    absent: their mount is the one that should be there.
    """
    return {
        spec.path: spec.volume
        for section in sections
        for spec in section.coordinator_files(settings)
        if isinstance(spec, CoordinatorFile)
    }


def admin_mounted_secrets(sections: tuple[Section, ...], settings: Settings) -> set[str]:
    """The Secrets a Section writes and the Admin mounts.

    These are the ones a subPath mount would ruin. A subPath mount is frozen at pod
    creation, and these files have to change *under a running pod*: the access-control
    rules are re-read on a timer, and a new client certificate has to appear in a directory
    that is already mounted.

    The files Apchi mounts itself are the opposite case and are deliberately absent. Apchi
    mounts a single key with subPath on purpose, replaces that mount when the file changes,
    and the Rollout that follows is what makes the new content live -- nothing has to reach
    a pod that is staying.
    """
    return {
        spec.secret
        for section in sections
        for spec in section.coordinator_files(settings)
        if isinstance(spec, CoordinatorDirectory)
    }

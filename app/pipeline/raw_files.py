"""Which paths an Admin may write a raw file at (§14).

Three refusals, and each one exists because the alternative is worse than being told no.

**A path Apchi owns.** The six Sections render files at known paths. A raw file at one of them
would not be an escape hatch -- it would silently replace generated configuration, and the
worst case is specific: overwriting the generated access-control rules turns invariant 8's
fail-closed authorization into a silently open Cluster, with the Operator's grants still
listed in the API so nothing looks wrong. "Admin wins on conflict" (§14) governs a *property*
an Admin sets inside a file Apchi generates; it does not extend to replacing the file.

**A path the deployment cannot mount a single file at.** Precondition 6 already refuses a
single-file mount whose parent is covered by a whole-directory mount: the kubelet has no file
to bind onto and runc fails the container with "not a directory". A deployment that mounts
`/etc/trino` as one volume therefore cannot receive raw files there -- and an Admin should
learn that when they write the file, not minutes later from an Apply that failed.

**A path outside Trino's configuration directory.** This is an escape hatch for Trino
configuration, not a way to write to a container's filesystem.

The first is derived from the Sections rather than listed, so a seventh Section is guarded
without anyone remembering to add it here.
"""

import hashlib
import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from app.adapters.kubernetes import KubernetesAdapter
from app.api.errors import UnprocessablePayload
from app.config import Settings
from app.pipeline import preconditions
from app.sections.admin import AdminValues
from app.sections.base import CoordinatorDirectory, CoordinatorFile
from app.sections.raw_files import CONFIG_DIR, MAX_FILES, normalised, within_config_dir
from app.sections.registry import REGISTERED

if TYPE_CHECKING:  # a Cluster carries the adapters and the Admin values together
    from app.sections.base import Cluster

logger = logging.getLogger(__name__)


def _owner_of(path: str, settings: Settings) -> str | None:
    """What Apchi already generates at this path, if anything.

    Two kinds, and both have to be refused for different reasons:

    * a **file** Apchi mounts itself (`CoordinatorFile`) -- a raw file there would be a second
      mount at one path, which precondition 4 already refuses at Apply time
    * anything **inside a directory** Apchi fills (`CoordinatorDirectory`) -- the Admin mounts
      that Secret as a whole volume, so Apchi owns everything in it and a raw file would
      either be clobbered by the next render or never appear at all

    Checking only the first was the gap: `owned_paths` answers "what does Apchi mount", and
    the question here is "what does Apchi generate", which is the larger set.

    Note what is *not* here. `/etc/trino/access-control.properties` is the **Admin's** file --
    it points Trino at the rules directory Apchi fills -- so an Admin may write it raw. If
    they leave out the refresh period, precondition 5 refuses the Apply, which is "if it works
    it works" working as intended.
    """
    candidate = PurePosixPath(path)
    for section in REGISTERED:
        for spec in section.coordinator_files(settings):
            declared = PurePosixPath(spec.path)
            if isinstance(spec, CoordinatorFile) and candidate == declared:
                return f"{section.name} ({spec.volume})"
            if isinstance(spec, CoordinatorDirectory) and candidate.is_relative_to(declared):
                return f"{section.name} ({spec.secret})"
    return None


async def refuse_unusable(
    path: str, kubernetes: KubernetesAdapter, settings: Settings, existing: int
) -> str:
    """The normalised path, or `UnprocessablePayload` saying why it cannot be used.

    Checked at write time rather than at Apply. An Admin reaching for this is usually in the
    middle of an incident, and a refusal that arrives minutes later from a failed Apply is a
    refusal that cost them the thing they were trying to fix.
    """
    candidate = normalised(path)

    if not within_config_dir(candidate):
        raise UnprocessablePayload(
            f"{candidate!r} is not under {CONFIG_DIR}. Raw files are an escape hatch for "
            "Trino's configuration, not a way to write elsewhere in the container.",
            details=[{"property": "path", "problem": f"must be under {CONFIG_DIR}"}],
        )

    if (volume := _owner_of(candidate, settings)) is not None:
        raise UnprocessablePayload(
            f"Apchi generates {candidate!r} itself, for the {volume} Section, so a raw file "
            "there would replace configuration an Operator owns rather than add to it. Set "
            "the properties you need in a file Apchi does not write, or change the Section "
            "that owns this one.",
            details=[{"property": "path", "problem": f"generated by Apchi, for {volume}"}],
        )

    if existing >= MAX_FILES:
        raise UnprocessablePayload(
            f"There are already {existing} raw files, which is the limit. An escape hatch "
            "with more than that in it has stopped being an escape hatch; fold what you can "
            "into fewer files, or ask for the Section that should own this.",
            details=[{"property": "path", "problem": f"at the limit of {MAX_FILES} raw files"}],
        )

    await _refuse_if_unmountable(candidate, kubernetes, settings)
    return candidate


async def _refuse_if_unmountable(
    path: str, kubernetes: KubernetesAdapter, settings: Settings
) -> None:
    """Refuse a path this deployment cannot deliver a single file to.

    The same condition precondition 6 enforces before an Apply, asked earlier and about one
    path. Read from the live pod template, because the answer is a property of how the
    platform mounted `/etc/trino` and not of anything Apchi decided.

    A Cluster Apchi cannot read the template of is **not** a refusal: the Admin's file is
    stored and the Apply's own preconditions will say so with everything else that is wrong.
    Refusing here on an unreachable API would make an incident worse for a reason that has
    nothing to do with the file.
    """
    try:
        spec = preconditions.pod_spec(
            await kubernetes.deployment_pod_spec(settings.coordinator_deployment_name)
        )
    except Exception:
        logger.warning(
            "could not read the coordinator's pod template to check a raw file's path; "
            "the Apply's preconditions will check it",
            extra={"raw_file": path},
        )
        return

    parent = str(PurePosixPath(path).parent)
    volumes = {volume.name: volume for volume in spec.volumes}
    for container in spec.containers:
        for mount in container.volume_mounts:
            if mount.sub_path:
                continue
            volume = volumes.get(mount.name)
            if volume is None or not volume.read_only_by_nature:
                continue
            if _covers(mount.mount_path, parent):
                raise UnprocessablePayload(
                    f"This deployment mounts {mount.name!r} as a whole directory at "
                    f"{mount.mount_path}, which covers {parent}. Apchi delivers a raw file by "
                    f"mounting that one file, and a single-file mount inside a directory "
                    f"volume has nothing to bind onto -- the container would fail to start "
                    f'with "not a directory". Put the file somewhere not covered by a '
                    f"directory mount, or have the platform mount {parent} differently.",
                    details=[
                        {
                            "property": "path",
                            "problem": f"{parent} is covered by the {mount.name} directory mount",
                        }
                    ],
                )


def _covers(mount_path: str, directory: str) -> bool:
    """Whether a mount at `mount_path` sits at or above `directory`.

    The same rule as `preconditions._covers`, and deliberately a copy rather than an import:
    that module is the Apply's gate and this one is a request-time check, and a shared helper
    between them would invite one to start importing the other's problem list.
    """
    normalised_mount = mount_path.rstrip("/")
    return directory == normalised_mount or directory.startswith(f"{normalised_mount}/")


def secret_key(path: str) -> str:
    """The Secret key one raw file is stored under.

    Not the filename, which is what a Section's `CoordinatorFile` uses. Raw files sit at
    arbitrary paths under /etc/trino, so two of them can share a basename --
    `/etc/trino/a/x.properties` and `/etc/trino/b/x.properties` -- and a key derived from the
    name alone would silently deliver one file to both places.

    A digest of the full path makes it unique; the basename is kept on the end so that
    `kubectl describe secret` is readable by somebody who did not write this. The key never
    reaches the container: a subPath mount takes its filename from the mount path.
    """
    digest = hashlib.sha256(path.encode()).hexdigest()[:12]
    return f"{digest}-{PurePosixPath(path).name}"


def planned(values: AdminValues) -> dict[str, str]:
    """Every raw file to deliver, by the path it is delivered at."""
    return dict(values.raw_files)


async def deliver(cluster: "Cluster") -> None:
    """Write the raw files Secret and mount each file -- or unmount what is gone.

    Not a Section, and deliberately not made one. Invariant 9 keeps Admin values out of the
    Candidate and the Snapshot model, and a Section is defined by participating in both; a
    seventh Section whose resources were never recorded would be a Section in name only and
    would have to be special-cased in every walk of the registry.

    So this mirrors `files.deliver` rather than reusing it: one Secret, and a `subPath` mount
    per file on one volume Apchi owns. The volume arrives with the first raw file and leaves
    with the last, which is the same arrangement §13.6 already needs for the Event Listener
    and for the same reason -- "no raw files" can only be expressed by the mounts not being
    there, since Kubernetes turns a subPath mount of an absent key into a directory Trino
    dies reading.
    """
    desired = planned(cluster.admin)
    settings = cluster.settings
    await cluster.kubernetes.write_secret(
        settings.raw_files_secret_name,
        {secret_key(path): content for path, content in desired.items()},
    )

    mounted = await _mounted_paths(cluster)
    gone = sorted(mounted - set(desired))
    for path in sorted(desired):
        await cluster.kubernetes.mount_secret_file(
            settings.coordinator_deployment_name,
            settings.trino_container_name,
            settings.raw_files_volume_name,
            settings.raw_files_secret_name,
            path,
            secret_key(path),
        )
    if gone:
        await cluster.kubernetes.unmount_secret_files(
            settings.coordinator_deployment_name,
            settings.trino_container_name,
            settings.raw_files_volume_name,
            gone,
            # The volume goes with the last file. A volume left behind with no mounts is dead
            # weight on the Admin's pod template, and the API server rejects removing one
            # while a mount still names it -- so the two have to move together.
            drop_volume=not desired,
        )
    # Paths and counts, never content: §14 treats every raw value as a credential because
    # Apchi cannot tell which part of one is.
    logger.info(
        "delivered admin raw files",
        extra={"raw_files": sorted(desired), "raw_files_removed": gone},
    )


async def _mounted_paths(cluster: "Cluster") -> set[str]:
    """Which raw files the coordinator is currently mounting.

    Read from the live pod template rather than from a second record of what Apchi believes it
    mounted. §11 settled that question for rollback -- a diff of two durable records needs no
    history to stay honest -- and the same argument applies here: a mount an Admin removed by
    hand is corrected by the next Apply instead of drifting forever.
    """
    spec = preconditions.pod_spec(
        await cluster.kubernetes.deployment_pod_spec(cluster.settings.coordinator_deployment_name)
    )
    return {
        mount.mount_path
        for container in spec.containers
        for mount in container.volume_mounts
        if mount.name == cluster.settings.raw_files_volume_name and mount.sub_path
    }


async def changed(cluster: "Cluster") -> bool:
    """Whether delivering would change what the coordinator is holding.

    Raw files are read once at start -- that is what makes them the class §14 calls most able
    to stop Trino booting -- so any change needs a Rollout. An Admin Apply moves nothing an
    Operator owns, so the restart decision cannot come from a Section's plan; §14 settles it
    by comparing the rendered file with what the coordinator actually has, and this is that
    comparison for raw files.
    """
    desired = planned(cluster.admin)
    if set(desired) != await _mounted_paths(cluster):
        return True
    live = await cluster.kubernetes.read_secret_if_present(cluster.settings.raw_files_secret_name)
    return {secret_key(p): c for p, c in desired.items()} != {
        key: value for key, value in (live or {}).items()
    }

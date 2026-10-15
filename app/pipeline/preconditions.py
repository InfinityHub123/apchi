"""Preconditions Apchi asserts on the Trino deployment.

Apchi does not own the Trino manifests -- that would put it in the business of heap
sizes and node selectors, which belong to Admins. It asserts what it depends on
instead, and fails loudly when an assertion breaks. Checked before **every** Apply,
not once at startup, because a later chart change can reintroduce a violation
silently. See section 16.

Each check exists because breaking it produces a failure that does not look like its
cause: Apchi writes a Secret, the write succeeds, Apchi reports success, and Trino
never sees the change.
"""

import logging
from pathlib import PurePosixPath
from typing import Any

from app.adapters.kubernetes import KubernetesAdapter
from app.config import Settings
from app.pipeline.coordinator import Container, PodSpec, Volume, VolumeMount, content_at, provides
from app.pipeline.files import admin_mounted_secrets, owned_paths
from app.sections.event_listeners.generator import PATH_IN_DIR as EVENT_LISTENER_PATH_IN_DIR
from app.sections.permissions.generator import PROPERTIES_PATH, REFRESH_PERIOD
from app.sections.registry import REGISTERED
from app.sections.resource_groups.generator import MANAGER_PATH as RESOURCE_GROUPS_MANAGER_PATH
from app.sections.resource_groups.generator import RULES_IN_DIR as RESOURCE_GROUPS_RULES_IN_DIR

__all__ = [
    "Container",
    "PodSpec",
    "PreconditionFailed",
    "Volume",
    "VolumeMount",
    "check",
    "pod_spec",
]

logger = logging.getLogger(__name__)


class PreconditionFailed(Exception):
    """The deployment cannot support what Apchi is about to do."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(" ".join(problems))


#: The directory `access-control.properties` sits in, for the message when nothing provides
#: either it or the file.
_PROPERTIES_DIR = str(PurePosixPath(PROPERTIES_PATH).parent)

#: Where `event-listener.config-files` lives when a deployment sets it.
_CONFIG_PROPERTIES = "/etc/trino/config.properties"


def _covers(mount_path: str, directory: str) -> bool:
    """Whether a mount at `mount_path` sits at or above `directory`."""
    normalised = mount_path.rstrip("/")
    return directory == normalised or directory.startswith(f"{normalised}/")


async def check(kubernetes: KubernetesAdapter, spec: PodSpec, settings: Settings) -> None:
    """Raises PreconditionFailed listing everything wrong, not just the first thing.

    Takes the adapter because one of these cannot be answered from the pod template alone:
    whether Trino was told to re-read the rules file Apchi writes is in the Admin's
    configuration, which has to be read.
    """
    volumes = {volume.name: volume for volume in spec.volumes}
    # Read from the registry rather than named here, so a Section that starts writing a
    # whole-volume Secret is guarded without anyone remembering to add it. The catalog seed
    # is not a Section's file -- an initContainer reads it, not Trino -- so it is named.
    apchi_managed = admin_mounted_secrets(REGISTERED, settings) | {settings.catalog_secret_name}
    problems: list[str] = []

    # 1. Apchi-managed files must not be subPath-mounted. Kubernetes documents that a
    #    subPath volume mount never receives updates: the file is frozen at pod
    #    creation, permanently, and Apchi's writes would go nowhere visible. Only the
    #    whole-volume files are checked: the ones Apchi mounts itself are single keys
    #    mounted with subPath on purpose, and a Rollout is what makes their content live.
    for container in spec.every_container:
        for mount in container.volume_mounts:
            volume = volumes.get(mount.name)
            if volume is None or mount.sub_path is None:
                continue
            if volume.secret_name in apchi_managed:
                problems.append(
                    f"Container {container.name!r} mounts Secret "
                    f"{volume.secret_name!r} at {mount.mount_path} with subPath "
                    f"{mount.sub_path!r}. A subPath mount never receives updates, so "
                    "Apchi would write this file and Trino would never see the change. "
                    "Mount the whole volume instead."
                )

    # 2. The catalog seed initContainer copies the durable catalogs into the store
    #    directory before Trino reads it. Without it the Cluster comes up empty.
    seeds = [
        container.name
        for container in spec.init_containers
        for mount in container.volume_mounts
        if volumes.get(mount.name) is not None
        and volumes[mount.name].secret_name == settings.catalog_secret_name
    ]
    if not seeds:
        problems.append(
            f"No initContainer mounts the catalog seed Secret "
            f"{settings.catalog_secret_name!r}. Without one the coordinator starts with "
            "an empty catalog store and every catalog in the latest Snapshot is missing."
        )

    # 3. Nothing read-only may be mounted over the store directory. An emptyDir there
    #    is correct and is what the seed design expects; a Secret or ConfigMap is not,
    #    because Trino writes this directory itself and those mounts are read-only.
    for container in spec.containers:
        for mount in container.volume_mounts:
            volume = volumes.get(mount.name)
            if volume is None or not volume.read_only_by_nature:
                continue
            if _covers(mount.mount_path, settings.catalog_store_dir):
                problems.append(
                    f"Container {container.name!r} mounts a read-only volume "
                    f"{mount.name!r} at {mount.mount_path}, which covers the catalog "
                    f"store {settings.catalog_store_dir}. Trino writes that directory "
                    "itself, so every CREATE CATALOG would fail."
                )

    # 4. Every file a Section declares is Apchi's to mount. An Admin mounting something
    #    else at one of those paths would be fighting Apchi over one file, and whichever of
    #    them wrote last would win silently. Read from the registry rather than named here,
    #    so a new file-owning Section is guarded without anyone remembering to add it.
    apchi_owned = owned_paths(REGISTERED, settings)
    for container in spec.containers:
        for mount in container.volume_mounts:
            expected = apchi_owned.get(mount.mount_path)
            if expected is not None and mount.name != expected:
                problems.append(
                    f"Container {container.name!r} mounts {mount.name!r} at "
                    f"{mount.mount_path}, which is a file Apchi delivers. Apchi adds and "
                    f"removes its own volume {expected!r} there; remove this mount."
                )

    # 5. Trino must be told to re-read the rules. Without `security.refresh-period` it reads
    #    them once at startup and never again, so Apchi would write a permission change,
    #    report success, and the Cluster would never see it (§13.4). The one precondition
    #    that reads the Admin's configuration rather than their pod template.
    problems.extend(await _refresh_period_problems(kubernetes, spec, volumes))

    # 6. Apchi's single-file mounts need a parent directory the kubelet can bind into. A
    #    subPath mount of one key at a path inside a ConfigMap or Secret directory mount has
    #    no file to bind onto -- the key is not in that read-only volume -- and runc fails
    #    the container with "not a directory". The pod never starts, so Trino logs nothing
    #    while the Apply that added the mount reported success. Verified against the
    #    official trino/trino chart, which mounts the whole of /etc/trino (§16).
    problems.extend(_directory_mounts_over_apchis_files(spec, volumes, apchi_owned))

    # 7. A pointer the deployment claims to provide is really there, and really names the
    #    directory Apchi fills. Without this an Admin who turns the mode on and forgets the
    #    property gets a Section that writes files nobody reads: the Apply succeeds and the
    #    Cluster behaves as if the Section were empty (#125).
    problems.extend(await _pointer_problems(kubernetes, spec, settings))

    if problems:
        raise PreconditionFailed(problems)
    logger.debug("deployment preconditions hold")


def _directory_mounts_over_apchis_files(
    spec: PodSpec, volumes: dict[str, Volume], owned: dict[str, str]
) -> list[str]:
    """Read-only volumes mounted at or above the directory any Apchi file lives in.

    Every declared path is checked, not only the ones the current Candidate would mount.
    Three of today's four appear the first time an Operator configures their Section, so
    checking lazily would leave a Cluster running for months and then crashlooping on that
    edit -- which is the failure this exists to refuse, arriving later and looking like the
    Operator's doing.

    Only read-only volumes, because that is the case the "not a directory" failure was
    verified for. A writable parent -- an emptyDir over the config directory -- is left
    alone rather than guessed at: refusing a deployment that may work is the opposite of
    what these checks are for.
    """
    problems: list[str] = []
    for container in spec.containers:
        for mount in container.volume_mounts:
            volume = volumes.get(mount.name)
            if volume is None or not volume.read_only_by_nature:
                continue
            # At or above the *parent*. A mount at the file path itself is Apchi's own, and
            # a mount below the parent is a nested directory, which the kubelet layers
            # happily -- the official chart's own /etc/trino/catalog is one.
            covered = sorted(
                path for path in owned if _covers(mount.mount_path, str(PurePosixPath(path).parent))
            )
            if not covered:
                continue
            problems.append(
                f"Container {container.name!r} mounts {mount.name!r} as a directory at "
                f"{mount.mount_path}, and Apchi mounts {', '.join(covered)} inside it. "
                "Kubernetes cannot place a single file inside a read-only ConfigMap or "
                "Secret directory: the key is not in that volume, so there is nothing to "
                'bind onto and the container fails to start with "not a directory". Trino '
                "logs nothing, because it never runs. Mount the files under "
                f"{mount.mount_path} one at a time instead of mounting the whole directory."
            )
    return problems


async def _refresh_period_problems(
    kubernetes: KubernetesAdapter, spec: PodSpec, volumes: dict[str, Volume]
) -> list[str]:
    """Whether the access-control properties Trino reads set a refresh period.

    Found through the pod template rather than configured in Apchi: whichever volume
    provides Trino's `access-control.properties` is the file in force, and its content is in
    the ConfigMap or Secret behind that volume. Apchi reads it and nothing else from there --
    the rest of that file is the Admin's business.

    "Provides" means the file itself or the directory containing it, and getting that wrong
    refused the official Trino chart for a file that was mounted all along. `coordinator`
    owns that question now, because Adoption asks it of every path rather than this one.
    """
    for container in spec.containers:
        mount = provides(container, PROPERTIES_PATH)
        if mount is None:
            continue
        if volumes.get(mount.name) is None:
            continue
        content = await content_at(kubernetes, spec, PROPERTIES_PATH, container.name)
        if content is None:
            return [
                f"Container {container.name!r} mounts {mount.name!r} at "
                f"{mount.mount_path}, which should provide {PROPERTIES_PATH}, but Apchi "
                f"cannot read what is in it, so it cannot tell whether {REFRESH_PERIOD} "
                "is set."
            ]
        if any(line.strip().startswith(f"{REFRESH_PERIOD}=") for line in content.splitlines()):
            return []
        return [
            f"{PROPERTIES_PATH} does not set {REFRESH_PERIOD}. Without it Trino reads "
            "the access-control rules once at startup and never again, so Apchi would "
            "write a permission change, report success, and the Cluster would never "
            "see it."
        ]
    return [
        f"Nothing provides {PROPERTIES_PATH} -- neither that path nor the "
        f"{_PROPERTIES_DIR} directory is mounted -- so Trino is not configured to read "
        "the access-control rules Apchi writes. Every permission Apchi applies would be "
        "written and ignored."
    ]


async def _pointer_problems(
    kubernetes: KubernetesAdapter, spec: PodSpec, settings: Settings
) -> list[str]:
    """Whether the pointers the deployment claims to provide are really there (#125).

    An Admin who turns one of these on and forgets the property gets a Section that writes
    files nobody reads: the Apply succeeds, the Secret is correct, and the Cluster behaves as
    if the Section were empty. That is the failure shape §16 exists to catch, and it is worth
    one read of the Admin's own configuration to refuse it instead.

    Read from the Cluster rather than trusted, for the same reason precondition 4 reads
    `access-control.properties`: the setting says what Apchi was told, and only the file says
    what Trino will do.
    """
    problems: list[str] = []
    if settings.resource_groups_properties_from_deployment:
        problems.extend(
            await _pointer_problem(
                kubernetes,
                spec,
                holder=RESOURCE_GROUPS_MANAGER_PATH,
                prop="resource-groups.config-file",
                expected=RESOURCE_GROUPS_RULES_IN_DIR,
                setting="resource_groups_properties_from_deployment",
            )
        )
    if settings.event_listener_config_files_from_deployment:
        problems.extend(
            await _pointer_problem(
                kubernetes,
                spec,
                holder=_CONFIG_PROPERTIES,
                prop="event-listener.config-files",
                expected=EVENT_LISTENER_PATH_IN_DIR,
                setting="event_listener_config_files_from_deployment",
            )
        )
    return problems


async def _pointer_problem(
    kubernetes: KubernetesAdapter,
    spec: PodSpec,
    *,
    holder: str,
    prop: str,
    expected: str,
    setting: str,
) -> list[str]:
    """One pointer: the file that should hold it, the property, and where it must point."""
    for container in spec.containers:
        if provides(container, holder) is None:
            continue
        content = await content_at(kubernetes, spec, holder, container.name)
        if content is None:
            # Either the volume covering this path does not hold the file, or Apchi cannot
            # read the ConfigMap or Secret behind it. Both are reported together because the
            # fix is the same -- put the file there -- and because telling them apart would
            # mean distinguishing "key absent" from "read failed" one layer down, which is
            # more machinery than the message is worth.
            return [
                f"Apchi is configured with {setting}, so the deployment must provide {prop} "
                f"in {holder}. A volume covers that path but Apchi found no file there, so "
                f"either it is not in that volume or Apchi cannot read it. Set "
                f"{prop}={expected} in the file mounted at {holder}, or turn {setting} off "
                f"and let Apchi mount the file itself."
            ]
        named = _property_in(content, prop)
        if named is None:
            return [
                f"{holder} does not set {prop}, but Apchi is configured with {setting} and "
                f"so writes nothing at the path Trino reads by default. Every "
                f"{prop.split('.')[0]} Apchi applies would be written and ignored. Set "
                f"{prop}={expected}, or turn {setting} off and let Apchi mount the file "
                f"itself."
            ]
        if named != expected:
            return [
                f"{holder} sets {prop}={named}, which is not {expected} -- the directory "
                f"Apchi fills. Trino would read a file Apchi never writes, so the Section "
                f"would look applied and have no effect."
            ]
        return []
    return [
        f"Nothing provides {holder}, but Apchi is configured with {setting} and depends on "
        f"the deployment setting {prop} there. Mount it, or turn {setting} off."
    ]


def _property_in(content: str, prop: str) -> str | None:
    """A Java properties value, or None. Last wins, as Trino's own loader does."""
    found = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{prop}="):
            found = stripped.split("=", 1)[1].strip()
    return found


def pod_spec(raw: dict[str, Any]) -> PodSpec:
    return PodSpec.model_validate(raw)

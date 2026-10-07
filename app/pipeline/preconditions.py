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

from pydantic import AliasPath, BaseModel, ConfigDict, Field

from app.adapters.kubernetes import KubernetesAdapter
from app.config import Settings
from app.pipeline.files import admin_mounted_secrets, owned_paths
from app.sections.permissions.generator import PROPERTIES_PATH, REFRESH_PERIOD
from app.sections.registry import REGISTERED

logger = logging.getLogger(__name__)


class PreconditionFailed(Exception):
    """The deployment cannot support what Apchi is about to do."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(" ".join(problems))


# These mirror Kubernetes' own schema, so they ignore unknown fields rather than
# rejecting them: a PodSpec has hundreds of fields and none of the rest concern Apchi.
class _K8sModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Volume(_K8sModel):
    name: str
    secret_name: str | None = Field(
        default=None, validation_alias=AliasPath("secret", "secretName")
    )
    config_map_name: str | None = Field(
        default=None, validation_alias=AliasPath("configMap", "name")
    )

    @property
    def read_only_by_nature(self) -> bool:
        """Secret and ConfigMap volumes are always mounted read-only. That is the
        finding the whole catalog seed design rests on (§7.1)."""
        return self.secret_name is not None or self.config_map_name is not None


class VolumeMount(_K8sModel):
    name: str
    mount_path: str = Field(validation_alias="mountPath")
    sub_path: str | None = Field(default=None, validation_alias="subPath")


class Container(_K8sModel):
    name: str
    volume_mounts: list[VolumeMount] = Field(default_factory=list, validation_alias="volumeMounts")


class PodSpec(_K8sModel):
    containers: list[Container] = Field(default_factory=list)
    init_containers: list[Container] = Field(
        default_factory=list, validation_alias="initContainers"
    )
    volumes: list[Volume] = Field(default_factory=list)

    @property
    def every_container(self) -> list[Container]:
        return [*self.init_containers, *self.containers]


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

    if problems:
        raise PreconditionFailed(problems)
    logger.debug("deployment preconditions hold")


async def _refresh_period_problems(
    kubernetes: KubernetesAdapter, spec: PodSpec, volumes: dict[str, Volume]
) -> list[str]:
    """Whether the access-control properties Trino reads set a refresh period.

    Found through the pod template rather than configured in Apchi: whichever volume
    provides Trino's `access-control.properties` is the file in force, and its content is
    in the ConfigMap or Secret behind that volume. Apchi reads it and nothing else from
    there -- the rest of that file is the Admin's business.

    "Provides" is the subtle part, and getting it wrong refused the official Trino chart.
    A deployment may mount the file on its own, which is what Apchi's reference deployment
    does, or mount the whole configuration directory that contains it, which is what the
    official chart does and what most charts do. Both are the file in force, so both are
    looked for -- and the more specific mount wins, because that is how the kubelet layers
    them.
    """
    for container in spec.containers:
        mount = _provides_properties(container)
        if mount is not None:
            volume = volumes.get(mount.name)
            if volume is None:
                continue
            content = await _content_of(kubernetes, volume, mount)
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


#: The directory `access-control.properties` sits in, as the deployment sees it.
_PROPERTIES_DIR = str(PurePosixPath(PROPERTIES_PATH).parent)
#: The key holding it, in either kind of mount: a ConfigMap key cannot contain a slash,
#: so a directory mount names the file by its basename and nothing deeper is possible.
_PROPERTIES_KEY = PurePosixPath(PROPERTIES_PATH).name


def _provides_properties(container: Container) -> VolumeMount | None:
    """The mount that puts `access-control.properties` in the container, if any.

    Either the file itself or the directory holding it. The longest matching mount path
    wins: a deployment that mounts the directory *and* overlays the single file is
    serving the overlay, and so is Trino.
    """
    candidates = [
        mount
        for mount in container.volume_mounts
        if mount.mount_path.rstrip("/") in (PROPERTIES_PATH, _PROPERTIES_DIR)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda mount: len(mount.mount_path.rstrip("/")))


async def _content_of(
    kubernetes: KubernetesAdapter, volume: Volume, mount: VolumeMount
) -> str | None:
    """The file behind a mount, from whichever kind of volume carries it."""
    if volume.config_map_name is not None:
        data = await kubernetes.read_config_map(volume.config_map_name)
    elif volume.secret_name is not None:
        data = await kubernetes.read_secret(volume.secret_name)
    else:
        return None
    if data is None:
        return None
    if mount.mount_path.rstrip("/") == PROPERTIES_PATH:
        # The file mounted on its own: subPath names the key, and a whole-volume mount of
        # a single file takes the key named like it.
        return data.get(mount.sub_path or _PROPERTIES_KEY)
    # The directory. A subPath here would select a sub-directory of the volume, which a
    # ConfigMap cannot have, so there is nothing Apchi could resolve -- and saying so
    # beats guessing at the key.
    if mount.sub_path is not None:
        return None
    return data.get(_PROPERTIES_KEY)


def pod_spec(raw: dict[str, Any]) -> PodSpec:
    return PodSpec.model_validate(raw)

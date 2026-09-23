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
from typing import Any

from pydantic import AliasPath, BaseModel, ConfigDict, Field

from app.config import Settings

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


def check(spec: PodSpec, settings: Settings) -> None:
    """Raises PreconditionFailed listing everything wrong, not just the first thing."""
    volumes = {volume.name: volume for volume in spec.volumes}
    apchi_managed = {settings.catalog_secret_name, settings.access_control_secret_name}
    problems: list[str] = []

    # 1. Apchi-managed files must not be subPath-mounted. Kubernetes documents that a
    #    subPath volume mount never receives updates: the file is frozen at pod
    #    creation, permanently, and Apchi's writes would go nowhere visible.
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

    if problems:
        raise PreconditionFailed(problems)
    logger.debug("deployment preconditions hold")


def pod_spec(raw: dict[str, Any]) -> PodSpec:
    return PodSpec.model_validate(raw)

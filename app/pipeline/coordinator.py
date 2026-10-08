"""What the coordinator is actually running, read off its pod template.

Apchi cannot ask Trino how it is configured. `system.metadata.catalogs` gives a catalog's
name and connector and no properties; there is no `SHOW CREATE CATALOG` in 483; the loaded
access-control rules have no introspection surface at all. All verified against a running
coordinator while writing §15.

So the pod template, plus the Secrets and ConfigMaps behind it, is the only place a Cluster's
configuration can be read -- and reading it is two questions, not one. *Which volume provides
this path* is answered here, from the template alone. *What is in it* needs the Secret or
ConfigMap that volume names.

Both the preconditions (§16) and Adoption's discovery (§15) ask those questions, which is why
this is its own module rather than part of either.
"""

import logging
from pathlib import PurePosixPath

from pydantic import AliasPath, BaseModel, ConfigDict, Field

from app.adapters.kubernetes import KubernetesAdapter

logger = logging.getLogger(__name__)


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

    def volumes_by_name(self) -> dict[str, Volume]:
        return {volume.name: volume for volume in self.volumes}


class Source(BaseModel):
    """Where a path's content comes from: a volume, and the key inside it."""

    mount: VolumeMount
    volume: Volume
    #: The key in the Secret or ConfigMap. None when the volume is not one of those, or
    #: when the path sits deeper inside a directory mount than a key can express.
    key: str | None


def provides(container: Container, path: str) -> VolumeMount | None:
    """The mount that puts `path` in this container, if any.

    Either the file itself or a directory holding it. A deployment may well do both -- the
    official Trino chart mounts the whole of `/etc/trino` and Apchi overlays single files
    inside it -- and the kubelet layers them so the more specific one wins. So the longest
    matching mount path is the one serving the file, and so is the one to read.
    """
    wanted = path.rstrip("/")
    candidates = [
        mount
        for mount in container.volume_mounts
        if mount.mount_path.rstrip("/") == wanted
        or wanted.startswith(f"{mount.mount_path.rstrip('/')}/")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda mount: len(mount.mount_path.rstrip("/")))


def source_of(spec: PodSpec, container_name: str | None, path: str) -> Source | None:
    """Which volume provides `path`, and the key in it. None when nothing does.

    `container_name` of None means whichever container provides it, which is what Adoption
    wants: it is reading the Cluster's configuration, not auditing one container's mounts.
    """
    volumes = spec.volumes_by_name()
    for container in spec.containers:
        if container_name is not None and container.name != container_name:
            continue
        mount = provides(container, path)
        if mount is None:
            continue
        volume = volumes.get(mount.name)
        if volume is None:
            # A mount naming a volume the spec does not declare. The API server rejects
            # this, so it means the template was read mid-edit rather than that the
            # deployment is wrong.
            continue
        return Source(mount=mount, volume=volume, key=_key_for(mount, path))
    return None


def _key_for(mount: VolumeMount, path: str) -> str | None:
    """The Secret or ConfigMap key holding `path`, given the mount serving it."""
    mounted = mount.mount_path.rstrip("/")
    if mounted == path.rstrip("/"):
        # The file on its own: subPath names the key, and a whole-volume mount of a single
        # file takes the key named like it.
        return mount.sub_path or PurePosixPath(path).name
    relative = path.rstrip("/").removeprefix(f"{mounted}/")
    if mount.sub_path is not None or "/" in relative:
        # A ConfigMap or Secret key cannot contain a slash, so nothing deeper than one
        # level inside a directory mount can be resolved -- and a subPath directory mount
        # selects a sub-directory such a volume cannot have. Saying so beats guessing.
        return None
    return relative


async def _data_of(kubernetes: KubernetesAdapter, volume: Volume) -> dict[str, str] | None:
    if volume.config_map_name is not None:
        return await kubernetes.read_config_map(volume.config_map_name)
    if volume.secret_name is not None:
        return await kubernetes.read_secret(volume.secret_name)
    return None


async def content_at(
    kubernetes: KubernetesAdapter, spec: PodSpec, path: str, container: str | None = None
) -> str | None:
    """What the coordinator has at `path`, or None when Apchi cannot read it.

    None covers three different things on purpose -- nothing mounts the path, the volume is
    not a Secret or ConfigMap, or the key cannot be resolved. A caller that needs to tell
    them apart asks `source_of` first; most callers only need to know they have no content.
    """
    source = source_of(spec, container, path)
    if source is None or source.key is None:
        return None
    data = await _data_of(kubernetes, source.volume)
    if data is None:
        return None
    return data.get(source.key)


async def files_under(
    kubernetes: KubernetesAdapter, spec: PodSpec, directory: str, container: str | None = None
) -> dict[str, str] | None:
    """Every file in a directory the coordinator mounts, keyed by full path.

    For the Sections whose configuration is a directory rather than a file -- client
    certificates, and the catalog store -- where the question is not "what is at this path"
    but "what is in here", and only the volume knows.

    None when nothing mounts the directory, which is different from an empty dict: an empty
    Secret mounts as an empty directory, and for Client Certificates that is a legitimate
    "none configured" rather than a Cluster Apchi cannot read.
    """
    source = source_of(spec, container, directory)
    if source is None:
        return None
    if source.mount.mount_path.rstrip("/") != directory.rstrip("/"):
        # The directory is inside a larger mount. Its contents are keys of that volume
        # prefixed with a path a ConfigMap key cannot contain, so there is nothing to list.
        return None
    data = await _data_of(kubernetes, source.volume)
    if data is None:
        return None
    return {f"{directory.rstrip('/')}/{key}": value for key, value in data.items()}

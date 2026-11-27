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

from app.adapters.kubernetes import (
    CREDENTIAL_FILE_MODE,
    FORBIDDEN_CREDENTIAL_BITS,
    TRINO_GROUP,
    KubernetesAdapter,
)
from app.config import Settings
from app.pipeline.coordinator import Container, PodSpec, Volume, VolumeMount
from app.pipeline.files import admin_mounted_secrets, credential_secrets, owned_paths
from app.sections.registry import REGISTERED
from app.sections.server_configuration import LISTENER_DIR, ROLE_PATHS, Role, secret_name

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


_CONFIG_DIR = "/etc/trino"


def _covers(mount_path: str, directory: str) -> bool:
    """Whether a mount at `mount_path` sits at or above `directory`."""
    normalised = mount_path.rstrip("/")
    return directory == normalised or directory.startswith(f"{normalised}/")


async def check(kubernetes: KubernetesAdapter, spec: PodSpec, settings: Settings) -> None:
    """Raises PreconditionFailed listing everything wrong, not just the first thing.

    Takes the adapter because the workers' pod template is read as well as the
    coordinator's.
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

    # 5. Apchi's single-file mounts need a parent directory the kubelet can bind into. A
    #    subPath mount of one key at a path inside a ConfigMap or Secret directory mount has
    #    no file to bind onto -- the key is not in that read-only volume -- and runc fails
    #    the container with "not a directory". The pod never starts, so Trino logs nothing
    #    while the Apply that added the mount reported success. Verified against the
    #    official trino/trino chart, which mounts the whole of /etc/trino (§16).
    problems.extend(_directory_mounts_over_apchis_files(spec, volumes, apchi_owned))

    # 6. A volume holding a credential must be restricted, and still readable. The kubelet
    #    writes a Secret volume's files 0644 and root-owned unless the pod asks otherwise,
    #    so the default for a private key is world-readable -- and libpq refuses a key file
    #    like that outright, which makes this one visible as a broken connector rather than
    #    only as an exposure (#182). The only precondition that reads the worker as well,
    #    because it is the only one whose subject is on both Deployments (#184).
    problems.extend(await _credential_exposure_problems(kubernetes, spec, settings))

    # 7. Nothing under /etc/trino is mounted from a volume Apchi does not write. Apchi renders
    #    every file there (ADR-0010), and a file of anyone else's would either be replaced by
    #    Apchi's mount at the same path -- Kubernetes merges mounts by path, measured on kind --
    #    or would put back into the directory a second owner that Apchi cannot see.
    #    On both roles: the workers' files are Apchi's as well. And every file Apchi renders for
    #    a role must actually be mounted from Apchi's Secret, or Apchi would write it, report the
    #    change applied, and Trino would go on reading something else.
    problems.extend(_foreign_mounts_in_config_dir(spec, volumes, settings, "coordinator"))
    problems.extend(_unmounted_rendered_files(spec, settings, "coordinator"))
    worker = await _worker_spec(kubernetes, settings)
    if worker is not None:
        worker_volumes = {volume.name: volume for volume in worker.volumes}
        problems.extend(_foreign_mounts_in_config_dir(worker, worker_volumes, settings, "worker"))
        problems.extend(_unmounted_rendered_files(worker, settings, "worker"))

    if problems:
        raise PreconditionFailed(problems)
    logger.debug("deployment preconditions hold")


def _unmounted_rendered_files(spec: PodSpec, settings: Settings, role: Role) -> list[str]:
    """Files Apchi renders for this role that its Trino container does not mount from them."""
    ours = secret_name(role, settings)
    volumes = {volume.name: volume for volume in spec.volumes}
    problems: list[str] = []
    for container in spec.containers:
        if container.name != settings.trino_container_name:
            continue
        mounted = {
            mount.mount_path.rstrip("/"): volumes.get(mount.name)
            for mount in container.volume_mounts
        }
        if role == "coordinator":
            whole = {
                mount.mount_path.rstrip("/"): volumes.get(mount.name)
                for mount in container.volume_mounts
                if not mount.sub_path
            }
            volume = whole.get(LISTENER_DIR)
            if volume is None or volume.secret_name != ours:
                problems.append(
                    f"The coordinator does not mount Secret {ours!r} whole at {LISTENER_DIR}, "
                    "where Trino reads the Event Listener from when the Admin configures one. "
                    "A listener applied there would be named in config.properties and missing "
                    "on disk, and the coordinator would not start. Mount the Secret there as a "
                    "directory, as the chart does."
                )
        for path in ROLE_PATHS[role]:
            volume = mounted.get(path)
            if volume is None or volume.secret_name != ours:
                problems.append(
                    f"The {role} does not mount {path} from Secret {ours!r}, where Apchi renders "
                    "it. Trino would read something else there -- or nothing -- and every change "
                    "Apchi applied to it would be reported applied and never arrive. Mount each "
                    f"key of {ours!r} at its file, as the chart does."
                )
    return problems


def apchi_secrets(settings: Settings) -> set[str]:
    """Every Secret Apchi writes that a Trino pod may mount under /etc/trino."""
    return {
        spec.secret for section in REGISTERED for spec in section.coordinator_files(settings)
    } | {
        settings.server_configuration_secret_name,
        settings.worker_server_configuration_secret_name,
        settings.raw_files_secret_name,
    }


def _foreign_mounts_in_config_dir(
    spec: PodSpec, volumes: dict[str, Volume], settings: Settings, role: str
) -> list[str]:
    """Mounts in Trino's configuration directory that come from somewhere other than Apchi.

    Only the Trino container: a sidecar's filesystem is its own, and nothing it mounts reaches
    the files Trino reads. Judged by the Secret behind the volume rather than the volume's
    name, because the name is the deployment's choice and the Secret is the thing Apchi writes.
    """
    ours = apchi_secrets(settings)
    problems: list[str] = []
    for container in spec.containers:
        if container.name != settings.trino_container_name:
            continue
        for mount in container.volume_mounts:
            path = mount.mount_path.rstrip("/")
            if path != _CONFIG_DIR and not path.startswith(f"{_CONFIG_DIR}/"):
                continue
            volume = volumes.get(mount.name)
            if volume is not None and volume.secret_name in ours:
                continue
            source = (
                "no such volume"
                if volume is None
                else f"ConfigMap {volume.config_map_name!r}"
                if volume.config_map_name
                else f"Secret {volume.secret_name!r}"
                if volume.secret_name
                else "a volume that is neither a Secret nor a ConfigMap"
            )
            problems.append(
                f"The {role}'s container {container.name!r} mounts {mount.name!r} ({source}) at "
                f"{mount.mount_path}. Apchi renders every file under {_CONFIG_DIR}, so "
                "nothing else may be mounted there: set the properties through the Server "
                "Configuration, or write the file as a Raw File, and remove this mount."
            )
    return problems


async def _credential_exposure_problems(
    kubernetes: KubernetesAdapter, spec: PodSpec, settings: Settings
) -> list[str]:
    """Credential-bearing volumes either role would expose on disk.

    **Both Deployments**, like the check that every file is Apchi's. The rest are about what
    only a coordinator has -- the seed initContainer, the catalog store -- and asserting them
    on a worker would refuse every correct deployment. This one
    is about a private key, and a worker holds those: a connector presenting a certificate to
    a data source runs there (ADR-0005), so the chart mounts the certificate volume on both
    roles and a key leaks on a worker exactly as it does on a coordinator.
    """
    problems = _exposure_in(spec, "coordinator", settings)
    worker = await _worker_spec(kubernetes, settings)
    if worker is not None:
        problems.extend(_exposure_in(worker, "worker", settings))
    return problems


async def _worker_spec(kubernetes: KubernetesAdapter, settings: Settings) -> PodSpec | None:
    """The worker's pod template, or None when there is no such Deployment.

    Absent is tolerated rather than refused, because whether the Cluster has workers at all is
    Verification's question and it already answers it: `ready_replicas` reads the same
    Deployment on every Apply and raises on a name that does not resolve. Giving that
    requirement a second owner here would mean two different failures for one
    misconfiguration, and this precondition's subject is file modes.
    """
    raw = await kubernetes.deployment_pod_spec_if_present(settings.worker_deployment_name)
    if raw is None:
        logger.warning(
            "no worker deployment to check for exposed credentials",
            extra={"deployment": settings.worker_deployment_name},
        )
        return None
    return pod_spec(raw)


def _exposure_in(spec: PodSpec, role: str, settings: Settings) -> list[str]:
    """One role's credential-bearing volumes, and what is wrong with them.

    Two halves, because each is useless alone: without a mode the kubelet writes 0644, and
    without an `fsGroup` a restricted file is root-only and Trino -- uid 1000 -- cannot read
    its own keys. 0640 plus the group is the one combination that is both, and 0600 is not
    reachable at all: the kubelet ORs `roMask` 0440 into the mode of a readOnly mount, which
    was measured on a live cluster rather than inferred (§13.2).

    Every credential volume is the chart's to mount, so the fix for each is the chart's too:
    Apchi mounts none of them during an Apply.

    Both the absent mode and the permissive one are reported, and on a live cluster it is
    always the second: the API server fills `defaultMode` in with 0644 when a Secret volume
    does not ask, so a pod template read back from the API has no missing mode. The absent
    case is what a chart or a manifest on disk looks like, which is what
    `scripts/check_trino_chart.py` reads.
    """
    # The server configuration is the Admin's properties, and those can be credentials --
    # `internal-communication.shared-secret`, an OAuth client secret -- so it is held to
    # the same mode as everything else that carries one. The chart mounts it 0640.
    bearing = credential_secrets(REGISTERED, settings) | {
        settings.catalog_secret_name,
        settings.server_configuration_secret_name,
        settings.worker_server_configuration_secret_name,
    }
    mounted = [volume for volume in spec.volumes if volume.secret_name in bearing]
    if not mounted:
        return []

    if settings.restricted_pod_security:
        return _restricted_exposure(mounted, role)

    if spec.fs_group is None:
        return [
            f"The {role} mounts {', '.join(sorted(volume.name for volume in mounted))}, which "
            f"hold credentials, but sets no securityContext.fsGroup. A Secret volume's files "
            f"are owned by root whatever the container runs as, so without a group Trino "
            f"belongs to they can only be world-readable or unreadable. Set "
            f"securityContext.fsGroup to the group Trino runs as ({TRINO_GROUP})."
        ]

    # The volume is named rather than the Secret behind it, because the fix -- a file mode --
    # is made on the volume.
    problems: list[str] = []
    for volume in mounted:
        mode = volume.default_mode
        if mode is None:
            problems.append(
                f"The {role}'s {volume.name!r} volume holds credentials and sets no "
                f"defaultMode, so the kubelet writes its files 0644 -- readable by every "
                f"process in the pod, and a private key mounted like that is one libpq "
                f"refuses to use outright. Set defaultMode: 0{CREDENTIAL_FILE_MODE:o} on "
                f"that volume."
            )
        elif mode & FORBIDDEN_CREDENTIAL_BITS:
            problems.append(
                f"The {role}'s {volume.name!r} volume holds credentials with defaultMode "
                f"{mode:#o}, which grants world access, group write or group execute to a "
                f"credential. Set defaultMode: 0{CREDENTIAL_FILE_MODE:o} on that volume."
            )
    return problems


def _restricted_exposure(mounted: list[Volume], role: str) -> list[str]:
    """The same check where the platform forbids an `fsGroup`, as restricted-v2 does.

    There a credential can only be world-readable or unreadable -- the pod runs as an
    arbitrary uid in no group a file could be given -- so the 0640-plus-group rule above
    would refuse every deployment such a cluster can run. What stays refused is everything
    beyond reading: a writable or executable credential is wrong on any platform.
    """
    problems: list[str] = []
    for volume in mounted:
        if volume.default_mode is None:
            continue
        if volume.default_mode & _WRITE_OR_EXECUTE:
            problems.append(
                f"The {role}'s {volume.name!r} volume holds credentials with defaultMode "
                f"{volume.default_mode:#o}, which lets them be written or executed. Set "
                f"defaultMode: 0644 on that volume."
            )
    return problems


#: Group or world write or execute. Reading is what restricted-v2 leaves as the only way to
#: hand a mounted file to an arbitrary uid, so it is the one access not refused there.
_WRITE_OR_EXECUTE = 0o033


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


def pod_spec(raw: dict[str, Any]) -> PodSpec:
    return PodSpec.model_validate(raw)

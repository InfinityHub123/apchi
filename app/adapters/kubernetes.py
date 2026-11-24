"""Kubernetes access, behind one interface.

The official client is synchronous, so every call is wrapped in a threadpool here
and nowhere else: pipeline code and route handlers stay plain async and never
mention threads. Keeping the whole surface in one protocol is also what lets the
fast test tier substitute it while MongoDB and Trino stay real.
"""

import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: The mode for a Secret file that carries credentials.
#:
#: 0640 and not 0600, and that is not a compromise. A Secret volume's files are owned by
#: **root** whatever the container runs as -- only the group follows `fsGroup` -- so 0600
#: would be root-only and Trino, which runs as uid 1000, could not read its own private
#: keys. 0640 makes the file root-owned and group-readable, which is exactly what libpq
#: accepts for a key file: it refuses group-write, group-execute and any world bit, and
#: nothing else.
#:
#: Asking for 0600 is not even an error, it is a no-op: the kubelet ORs `roMask = 0440`
#: into every file's mode on a readOnly volume, so 0600 arrives as 0640 regardless. See
#: §13.2 for the measurements.
CREDENTIAL_FILE_MODE = 0o640

#: The `trino` user's group in the official image, verified by running `id` in it:
#: `uid=1000(trino) gid=1000(trino)`. It is the other half of the mode above -- a pod whose
#: `fsGroup` is this can read a file restricted to its group, and a pod with no `fsGroup`
#: cannot read one at all.
TRINO_GROUP = 1000

#: Mode bits no credential file may have: world access in any form, plus group write and
#: group execute. This is libpq's own rule for a root-owned key file, read from
#: `fe-secure-openssl.c`, and group *read* is deliberately absent from it -- which is the
#: only reason `CREDENTIAL_FILE_MODE` can be readable at all.
FORBIDDEN_CREDENTIAL_BITS = 0o037


class PodState(BaseModel):
    """Enough to decide whether to keep waiting, connect, or give up.

    `problem` is set only for conditions waiting cannot fix -- an image that will
    not pull, a container that will not start. Validation fails on it immediately
    rather than burning its whole timeout.
    """

    phase: str = "Pending"
    host: str | None = None
    port: int = 8080
    problem: str | None = None


class RolloutState(BaseModel):
    """Where a Deployment is in adopting its current pod template.

    `complete` is the question `kubectl rollout status` answers, asked through the API:
    the controller has seen this template, every replica has been replaced by one running
    it, and none is unavailable. Checking readiness alone would pass while old pods were
    still serving.
    """

    generation: int = 0
    observed_generation: int = 0
    replicas: int = 0
    updated: int = 0
    ready: int = 0
    unavailable: int = 0

    @property
    def complete(self) -> bool:
        return (
            self.observed_generation >= self.generation
            and self.updated == self.replicas
            and self.ready == self.replicas
            and self.unavailable == 0
        )

    def summary(self) -> str:
        return (
            f"generation {self.observed_generation}/{self.generation}, "
            f"updated {self.updated}/{self.replicas}, ready {self.ready}/{self.replicas}, "
            f"unavailable {self.unavailable}"
        )


#: Container states that no amount of waiting resolves.
_HOPELESS = frozenset(
    {
        "ImagePullBackOff",
        "ErrImagePull",
        "InvalidImageName",
        "CreateContainerConfigError",
        "CreateContainerError",
        "CrashLoopBackOff",
    }
)


@runtime_checkable
class KubernetesAdapter(Protocol):
    async def read_secret(self, name: str) -> dict[str, str]: ...

    async def read_secret_if_present(self, name: str) -> dict[str, str] | None: ...

    async def read_config_map(self, name: str) -> dict[str, str]: ...

    async def write_secret(self, name: str, data: dict[str, str]) -> None: ...

    async def ready_replicas(self, deployment: str) -> int: ...

    async def deployment_image(self, deployment: str, container: str) -> str: ...

    async def deployment_pod_spec(self, deployment: str) -> dict[str, Any]: ...

    async def deployment_pod_spec_if_present(self, deployment: str) -> dict[str, Any] | None: ...

    async def create_pod(self, manifest: dict[str, Any]) -> None: ...

    async def create_secret(
        self, name: str, data: dict[str, str], labels: dict[str, str]
    ) -> None: ...

    async def delete_secret(self, name: str) -> None: ...

    async def delete_secrets(self, label_selector: str) -> list[str]: ...

    async def pod_logs(self, name: str, tail: int) -> str: ...

    async def pod_state(self, name: str) -> PodState: ...

    async def delete_pod(self, name: str) -> None: ...

    async def delete_pods(self, label_selector: str) -> list[str]: ...

    async def restart_deployment(self, deployment: str, reason: str) -> None: ...

    async def rollout_state(self, deployment: str) -> RolloutState: ...

    async def mount_secret_file(
        self, deployment: str, container: str, volume: str, secret: str, path: str, key: str
    ) -> None: ...

    async def unmount_secret_files(
        self, deployment: str, container: str, volume: str, paths: Sequence[str], drop_volume: bool
    ) -> None: ...


class RealKubernetes:
    """Clients are built on first use, not in the constructor.

    Loading the config eagerly would make an unreachable cluster a startup crash.
    Apchi reports that through health instead, the same choice it makes for an
    unreachable MongoDB: a pod that crashloops on a transient blip loses its logs.
    """

    def __init__(self, namespace: str = "default") -> None:
        self._namespace = namespace
        self._core_api: Any = None
        self._apps_api: Any = None

    def _load(self) -> None:
        if self._core_api is not None:
            return
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        self._core_api = client.CoreV1Api()
        self._apps_api = client.AppsV1Api()

    @property
    def _core(self) -> Any:
        self._load()
        return self._core_api

    @property
    def _apps(self) -> Any:
        self._load()
        return self._apps_api

    async def _call(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """One API call in the threadpool, logged at DEBUG by verb, object and timing.

        Never the body, in either direction: a request or response here can be a Secret, and
        its values are base64 with no field name next to them for the redaction filter to
        recognise. The client's own debug logging prints exactly that, which is why it stays
        off and this exists.
        """
        from kubernetes.client.exceptions import ApiException

        target = _target(method.__name__, args, kwargs, self._namespace)
        started = time.monotonic()
        try:
            result = await run_in_threadpool(method, *args, **kwargs)
        except ApiException as exc:
            logger.debug(
                "kubernetes: %s %s -> %s in %dms",
                method.__name__,
                target,
                exc.status,
                _elapsed(started),
            )
            raise
        except Exception as exc:
            logger.debug(
                "kubernetes: %s %s -> %s in %dms",
                method.__name__,
                target,
                type(exc).__name__,
                _elapsed(started),
            )
            raise
        logger.debug("kubernetes: %s %s -> ok in %dms", method.__name__, target, _elapsed(started))
        return result

    async def read_secret(self, name: str) -> dict[str, str]:
        """A Secret's contents, or empty when there is no such Secret.

        Absence is not an error here: a Section that has never had content may have no Secret
        yet, and that is the same configuration as an empty one.
        """
        import base64

        secret = await self._read(self._core.read_namespaced_secret, name)
        if secret is None:
            return {}
        return {k: base64.b64decode(v).decode() for k, v in (secret.data or {}).items()}

    async def read_secret_if_present(self, name: str) -> dict[str, str] | None:
        """The same read, with absence distinguishable from emptiness.

        `read_secret` folds the two together, which is right for almost every caller. A
        Secret that does not exist cannot be mounted -- `MountVolume.SetUp failed ... secret
        not found` leaves the coordinator stuck in Init -- so a caller deciding whether to
        write one has to know which it is, and the distinction lives in the type.
        """
        import base64

        secret = await self._read(self._core.read_namespaced_secret, name)
        if secret is None:
            return None
        return {k: base64.b64decode(v).decode() for k, v in (secret.data or {}).items()}

    async def read_config_map(self, name: str) -> dict[str, str]:
        """A ConfigMap's contents, or empty when there is no such ConfigMap.

        Read-only: Apchi reads an Admin's ConfigMaps to check a precondition it cannot check
        any other way -- whether Trino was told to re-read the rules file.
        """
        config_map = await self._read(self._core.read_namespaced_config_map, name)
        return {} if config_map is None else dict(config_map.data or {})

    async def _read(self, read: Any, name: str) -> Any:
        """One read, with "there is no such object" distinguished from a real failure.

        Only a 404 is swallowed. A 403 means Apchi's Role is wrong and a 500 means the API
        server is unwell, and reporting either as an empty object would make a Cluster look
        unconfigured rather than unreadable.
        """
        from kubernetes.client.exceptions import ApiException

        try:
            return await self._call(read, name, self._namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    async def write_secret(self, name: str, data: dict[str, str]) -> None:
        """The Secret's contents become exactly `data`.

        A merge patch merges the map key by key, so a key that should disappear has
        to be sent explicitly as null. Without that a dropped Catalog would stay in
        the durable copy and come back at the next pod restart.
        """
        existing = await self.read_secret(name)
        body: dict[str, Any] = {"stringData": data}
        stale = {key: None for key in existing if key not in data}
        if stale:
            body["data"] = stale
        from kubernetes.client.exceptions import ApiException

        try:
            await self._call(
                self._core.patch_namespaced_secret,
                name,
                self._namespace,
                body,
            )
        except ApiException as exc:
            if exc.status != 404:
                raise
            # A patch cannot create, and a Secret Apchi writes for the first time does not
            # exist yet. Creating is the same operation from Apchi's side: the Secret's
            # contents become exactly `data` either way.
            await self._call(
                self._core.create_namespaced_secret,
                self._namespace,
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": name},
                    "stringData": data,
                },
            )

    async def ready_replicas(self, deployment: str) -> int:
        dep = await self._call(self._apps.read_namespaced_deployment, deployment, self._namespace)
        return int(dep.status.ready_replicas or 0)

    async def deployment_image(self, deployment: str, container: str) -> str:
        """Read from the live Deployment rather than configured separately, so the
        validation pod cannot drift from the version the Cluster actually runs."""
        dep = await self._call(self._apps.read_namespaced_deployment, deployment, self._namespace)
        for spec in dep.spec.template.spec.containers:
            if spec.name == container:
                return str(spec.image)
        raise LookupError(f"Deployment {deployment!r} has no container named {container!r}")

    async def deployment_pod_spec(self, deployment: str) -> dict[str, Any]:
        """The pod template's spec, as plain data.

        Serialised rather than handed over as client objects: the preconditions that
        read it live in `pipeline/`, which stays free of the Kubernetes client.
        """
        from kubernetes import client

        dep = await self._call(self._apps.read_namespaced_deployment, deployment, self._namespace)
        serialised = client.ApiClient().sanitize_for_serialization(dep.spec.template.spec)
        return dict(serialised)

    async def deployment_pod_spec_if_present(self, deployment: str) -> dict[str, Any] | None:
        """The same, with "there is no such Deployment" as an answer rather than an error.

        For the caller that is asking *about* a Deployment it does not require to exist, which
        today is the credential precondition reading the worker (#184). Only a 404 is
        swallowed, for the reason `_read` gives: a 403 means Apchi's Role is wrong and
        reporting that as "no workers" would turn a permissions problem into a silently
        skipped check.
        """
        from kubernetes.client.exceptions import ApiException

        try:
            return await self.deployment_pod_spec(deployment)
        except ApiException as exc:
            if exc.status != 404:
                raise
            return None

    async def restart_deployment(self, deployment: str, reason: str) -> None:
        """Patch the pod template's annotations, which is all a rollout restart is -- so it
        works through the API with no kubectl dependency. Any change to the template makes
        the controller replace every pod."""
        await self._patch_deployment(
            deployment,
            {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "apchi.dev/restarted-at": datetime.now(UTC).isoformat(),
                                "apchi.dev/restarted-because": reason,
                            }
                        }
                    }
                }
            },
        )

    async def rollout_state(self, deployment: str) -> RolloutState:
        dep = await self._call(self._apps.read_namespaced_deployment, deployment, self._namespace)
        status = dep.status
        return RolloutState(
            generation=int(dep.metadata.generation or 0),
            observed_generation=int(status.observed_generation or 0),
            replicas=int(dep.spec.replicas or 0),
            updated=int(status.updated_replicas or 0),
            ready=int(status.ready_replicas or 0),
            unavailable=int(status.unavailable_replicas or 0),
        )

    async def mount_secret_file(
        self, deployment: str, container: str, volume: str, secret: str, path: str, key: str
    ) -> None:
        """Mount one key of a Secret at one path. Idempotent: volumes merge by name and
        mounts by mountPath, so re-applying the same mount changes nothing -- and a
        template that does not change triggers no rollout.

        The file is restricted to the owning group **only where the deployment declares an
        `fsGroup`**, and that condition is the whole care here. These files can carry
        credentials -- an Event Listener's properties will hold a password in a JDBC URL --
        so a world-readable one is worth closing. But a Secret file is root-owned, so 0640
        without a group that Trino belongs to is a file Trino cannot read, and
        `event-listener.properties` unreadable is a coordinator that will not start. On a
        deployment with no `fsGroup` the mode is left alone: Apchi does not own that pod
        template, and hardening a file into an outage is worse than the exposure.
        """
        secret_volume: dict[str, Any] = {"secretName": secret}
        if await self._fs_group(deployment) is not None:
            secret_volume["defaultMode"] = CREDENTIAL_FILE_MODE
        await self._patch_deployment(
            deployment,
            {
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": [{"name": volume, "secret": secret_volume}],
                            "containers": [
                                {
                                    "name": container,
                                    "volumeMounts": [
                                        {
                                            "name": volume,
                                            "mountPath": path,
                                            "subPath": key,
                                            "readOnly": True,
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                }
            },
        )

    async def _fs_group(self, deployment: str) -> int | None:
        """The pod template's `fsGroup`, or None when it declares none.

        Read rather than configured: it is the Admin's pod template, and a setting Apchi
        was told about could disagree with the one the pod actually has.
        """
        spec = await self.deployment_pod_spec(deployment)
        group = (spec.get("securityContext") or {}).get("fsGroup")
        return int(group) if group is not None else None

    async def unmount_secret_files(
        self, deployment: str, container: str, volume: str, paths: Sequence[str], drop_volume: bool
    ) -> None:
        """Remove these mounts, and the volume with them when nothing is left on it.

        One patch for all of them, and `drop_volume` decided by the caller, because the two
        cannot be separated safely: removing a volume while another mount still names it is
        rejected by the API server ("volumeMounts[0].name ... not found"), and removing the
        last mount while leaving the volume behind leaves a volume nothing uses.

        Idempotent: deleting what is not there is the outcome the caller wanted.
        """
        await self._patch_deployment(
            deployment,
            {
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": (
                                [{"name": volume, "$patch": "delete"}] if drop_volume else []
                            ),
                            "containers": [
                                {
                                    "name": container,
                                    "volumeMounts": [
                                        {"mountPath": path, "$patch": "delete"} for path in paths
                                    ],
                                }
                            ],
                        }
                    }
                }
            },
        )

    async def _patch_deployment(self, deployment: str, patch: dict[str, Any]) -> None:
        await self._call(self._apps.patch_namespaced_deployment, deployment, self._namespace, patch)

    async def create_pod(self, manifest: dict[str, Any]) -> None:
        await self._call(self._core.create_namespaced_pod, self._namespace, manifest)

    async def create_secret(self, name: str, data: dict[str, str], labels: dict[str, str]) -> None:
        """Created rather than patched, because this one is born and dies with a pod."""
        await self._call(
            self._core.create_namespaced_secret,
            self._namespace,
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "labels": labels},
                "stringData": data,
            },
        )

    async def delete_secret(self, name: str) -> None:
        """Idempotent: one already gone is the outcome the caller wanted."""
        from kubernetes.client.exceptions import ApiException

        try:
            await self._call(self._core.delete_namespaced_secret, name, self._namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise

    async def delete_secrets(self, label_selector: str) -> list[str]:
        secrets = await self._call(
            self._core.list_namespaced_secret, self._namespace, label_selector=label_selector
        )
        names = [secret.metadata.name for secret in secrets.items]
        for name in names:
            await self.delete_secret(name)
        return names

    async def pod_logs(self, name: str, tail: int) -> str:
        """The pod's own account of why it stopped.

        Read for one reason: when a probe refuses to start, its log is the only place the
        reason exists. Trino writes nothing to the termination-log file Kubernetes would
        otherwise surface.
        """
        from kubernetes.client.exceptions import ApiException

        try:
            # `_preload_content=False` because the client's own deserialisation of this
            # endpoint is `str(response.data)` -- a `b'...'` repr with the newlines
            # escaped, which arrives as one enormous line that no error can be picked out
            # of. Taking the raw body and decoding it is the only way to get a log back.
            # An earlier attempt at this guarded on `isinstance(logs, bytes)`, which never
            # fires: what comes back is a str that *contains* the repr.
            response = await self._call(
                self._core.read_namespaced_pod_log,
                name,
                self._namespace,
                tail_lines=tail,
                _preload_content=False,
            )
            raw = response.data
            return raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        except ApiException:
            return ""

    async def pod_state(self, name: str) -> PodState:
        from kubernetes.client.exceptions import ApiException

        try:
            pod = await self._call(self._core.read_namespaced_pod, name, self._namespace)
        except ApiException as exc:
            if exc.status == 404:
                return PodState(phase="Missing", problem="the pod no longer exists")
            raise

        status = pod.status
        state = PodState(phase=str(status.phase or "Pending"), host=status.pod_ip or None)
        if state.phase == "Failed":
            state.problem = str(status.reason or "the pod failed")
            return state
        for container in status.container_statuses or []:
            waiting = container.state.waiting if container.state else None
            if waiting and waiting.reason in _HOPELESS:
                state.problem = f"{waiting.reason}: {waiting.message or 'no detail'}"
        return state

    async def delete_pod(self, name: str) -> None:
        """Idempotent: a pod already gone is the outcome the caller wanted."""
        from kubernetes.client.exceptions import ApiException

        try:
            await self._call(self._core.delete_namespaced_pod, name, self._namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise

    async def delete_pods(self, label_selector: str) -> list[str]:
        pods = await self._call(
            self._core.list_namespaced_pod, self._namespace, label_selector=label_selector
        )
        names = [pod.metadata.name for pod in pods.items]
        for name in names:
            await self.delete_pod(name)
        return names


def _target(verb: str, args: tuple[Any, ...], kwargs: dict[str, Any], namespace: str) -> str:
    """What a call acted on, in words: an object's name, or the selector a list used.

    A create is passed the namespace and then the whole object, so its name is read out of
    the object's metadata rather than from the arguments.
    """
    if "label_selector" in kwargs:
        return f"[{kwargs['label_selector']}]"
    if verb.startswith("create_") and len(args) >= 2 and isinstance(args[1], dict):
        return str(args[1].get("metadata", {}).get("name", "?"))
    named = [a for a in args if isinstance(a, str) and a != namespace]
    return named[0] if named else ""


def _elapsed(started: float) -> int:
    return round((time.monotonic() - started) * 1000)

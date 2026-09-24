"""Kubernetes access, behind one interface.

The official client is synchronous, so every call is wrapped in a threadpool here
and nowhere else: pipeline code and route handlers stay plain async and never
mention threads. Keeping the whole surface in one protocol is also what lets the
fast test tier substitute it while MongoDB and Trino stay real.
"""

from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel


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

    async def write_secret(self, name: str, data: dict[str, str]) -> None: ...

    async def ready_replicas(self, deployment: str) -> int: ...

    async def deployment_image(self, deployment: str, container: str) -> str: ...

    async def deployment_pod_spec(self, deployment: str) -> dict[str, Any]: ...

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

    async def unmount_secret_file(
        self, deployment: str, container: str, volume: str, path: str
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

    async def read_secret(self, name: str) -> dict[str, str]:
        import base64

        secret = await run_in_threadpool(self._core.read_namespaced_secret, name, self._namespace)
        return {k: base64.b64decode(v).decode() for k, v in (secret.data or {}).items()}

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
        await run_in_threadpool(
            self._core.patch_namespaced_secret,
            name,
            self._namespace,
            body,
        )

    async def ready_replicas(self, deployment: str) -> int:
        dep = await run_in_threadpool(
            self._apps.read_namespaced_deployment, deployment, self._namespace
        )
        return int(dep.status.ready_replicas or 0)

    async def deployment_image(self, deployment: str, container: str) -> str:
        """Read from the live Deployment rather than configured separately, so the
        validation pod cannot drift from the version the Cluster actually runs."""
        dep = await run_in_threadpool(
            self._apps.read_namespaced_deployment, deployment, self._namespace
        )
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

        dep = await run_in_threadpool(
            self._apps.read_namespaced_deployment, deployment, self._namespace
        )
        serialised = client.ApiClient().sanitize_for_serialization(dep.spec.template.spec)
        return dict(serialised)

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
        dep = await run_in_threadpool(
            self._apps.read_namespaced_deployment, deployment, self._namespace
        )
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
        template that does not change triggers no rollout."""
        await self._patch_deployment(
            deployment,
            {
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": [{"name": volume, "secret": {"secretName": secret}}],
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

    async def unmount_secret_file(
        self, deployment: str, container: str, volume: str, path: str
    ) -> None:
        """Remove that mount and its volume, leaving every other mount alone. Idempotent:
        deleting what is not there is the outcome the caller wanted."""
        await self._patch_deployment(
            deployment,
            {
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": [{"name": volume, "$patch": "delete"}],
                            "containers": [
                                {
                                    "name": container,
                                    "volumeMounts": [{"mountPath": path, "$patch": "delete"}],
                                }
                            ],
                        }
                    }
                }
            },
        )

    async def _patch_deployment(self, deployment: str, patch: dict[str, Any]) -> None:
        await run_in_threadpool(
            self._apps.patch_namespaced_deployment, deployment, self._namespace, patch
        )

    async def create_pod(self, manifest: dict[str, Any]) -> None:
        await run_in_threadpool(self._core.create_namespaced_pod, self._namespace, manifest)

    async def create_secret(self, name: str, data: dict[str, str], labels: dict[str, str]) -> None:
        """Created rather than patched, because this one is born and dies with a pod."""
        await run_in_threadpool(
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
            await run_in_threadpool(self._core.delete_namespaced_secret, name, self._namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise

    async def delete_secrets(self, label_selector: str) -> list[str]:
        secrets = await run_in_threadpool(
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
            logs = await run_in_threadpool(
                self._core.read_namespaced_pod_log, name, self._namespace, tail_lines=tail
            )
            return str(logs)
        except ApiException:
            return ""

    async def pod_state(self, name: str) -> PodState:
        from kubernetes.client.exceptions import ApiException

        try:
            pod = await run_in_threadpool(self._core.read_namespaced_pod, name, self._namespace)
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
            await run_in_threadpool(self._core.delete_namespaced_pod, name, self._namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise

    async def delete_pods(self, label_selector: str) -> list[str]:
        pods = await run_in_threadpool(
            self._core.list_namespaced_pod, self._namespace, label_selector=label_selector
        )
        names = [pod.metadata.name for pod in pods.items]
        for name in names:
            await self.delete_pod(name)
        return names

"""Kubernetes access, behind one interface.

The official client is synchronous, so every call is wrapped in a threadpool here
and nowhere else: pipeline code and route handlers stay plain async and never
mention threads. Keeping the whole surface in one protocol is also what lets the
fast test tier substitute it while MongoDB and Trino stay real.
"""

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

    async def create_pod(self, manifest: dict[str, Any]) -> None: ...

    async def pod_state(self, name: str) -> PodState: ...

    async def delete_pod(self, name: str) -> None: ...

    async def delete_pods(self, label_selector: str) -> list[str]: ...


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

    async def create_pod(self, manifest: dict[str, Any]) -> None:
        await run_in_threadpool(self._core.create_namespaced_pod, self._namespace, manifest)

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

"""Kubernetes access, behind one interface.

The official client is synchronous, so every call is wrapped in a threadpool here
and nowhere else: pipeline code and route handlers stay plain async and never
mention threads. Keeping the whole surface in one protocol is also what lets the
fast test tier substitute it while MongoDB and Trino stay real.
"""

from typing import Any, Protocol, runtime_checkable

from fastapi.concurrency import run_in_threadpool


@runtime_checkable
class KubernetesAdapter(Protocol):
    async def read_secret(self, name: str) -> dict[str, str]: ...

    async def write_secret(self, name: str, data: dict[str, str]) -> None: ...

    async def ready_replicas(self, deployment: str) -> int: ...


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

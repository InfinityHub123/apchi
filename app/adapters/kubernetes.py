"""Kubernetes access, behind one interface.

The official client is synchronous, so every call is wrapped in a threadpool here
and nowhere else: pipeline code and route handlers stay plain async and never
mention threads. Keeping the whole surface in one protocol is also what lets the
fast test tier substitute it while MongoDB and Trino stay real.
"""

from typing import Protocol, runtime_checkable

from fastapi.concurrency import run_in_threadpool


@runtime_checkable
class KubernetesAdapter(Protocol):
    async def read_secret(self, name: str) -> dict[str, str]: ...

    async def patch_secret(self, name: str, data: dict[str, str]) -> None: ...

    async def ready_replicas(self, deployment: str) -> int: ...


class RealKubernetes:
    def __init__(self, namespace: str = "default") -> None:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        self._core = client.CoreV1Api()
        self._apps = client.AppsV1Api()
        self._namespace = namespace

    async def read_secret(self, name: str) -> dict[str, str]:
        import base64

        secret = await run_in_threadpool(self._core.read_namespaced_secret, name, self._namespace)
        return {k: base64.b64decode(v).decode() for k, v in (secret.data or {}).items()}

    async def patch_secret(self, name: str, data: dict[str, str]) -> None:
        await run_in_threadpool(
            self._core.patch_namespaced_secret,
            name,
            self._namespace,
            {"stringData": data},
        )

    async def ready_replicas(self, deployment: str) -> int:
        dep = await run_in_threadpool(
            self._apps.read_namespaced_deployment, deployment, self._namespace
        )
        return int(dep.status.ready_replicas or 0)

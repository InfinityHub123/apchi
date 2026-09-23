"""Tier 2: the same HTTP API, but against a real Kubernetes cluster.

Skipped unless a cluster is reachable, so the fast suite stays runnable anywhere.
CI provides one via kind.

MongoDB still comes from testcontainers: what tier 2 adds is a real Kubernetes and
a real in-cluster Trino, not a different database.
"""

import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.kubernetes import PodState, RealKubernetes
from app.adapters.trino import Trino
from app.config import Settings
from app.main import create_app

COORDINATOR = "deploy/trino-coordinator"
TRINO_SERVICE = "svc/trino"
CATALOG_SEED_SECRET = "trino-catalog-seed"


def _cluster_available() -> bool:
    """Probed with kubectl rather than by constructing the adapter: the adapter now
    builds its clients lazily, so constructing one proves nothing."""
    try:
        subprocess.run(["kubectl", "cluster-info"], capture_output=True, timeout=30, check=True)
    except Exception:
        return False
    return True


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def kubectl(*args: str, timeout: float = 600) -> str:
    result = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=timeout, check=True
    )
    return result.stdout


class PortForward:
    """A kubectl port-forward that can be re-established.

    A forward attaches to whichever pod is behind the Service, so it dies with that
    pod. The restart test deliberately kills that pod, which is why this is an
    explicit object rather than a fixture that sets one up once.
    """

    def __init__(self, target: str, remote_port: int) -> None:
        self._target = target
        self._remote_port = remote_port
        self.port = _free_port()
        self._process: subprocess.Popen[bytes] | None = None

    def start(self, timeout: float = 120, await_trino: bool = True) -> None:
        """With await_trino, returns only once Trino is serving through the tunnel.
        Without it, returns as soon as the tunnel accepts a connection -- for a pod
        whose Trino is still starting and whose readiness the caller polls itself."""
        self._process = subprocess.Popen(
            ["kubectl", "port-forward", self._target, f"{self.port}:{self._remote_port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if await_trino:
            self._await_coordinator(timeout)
        else:
            self._await_tunnel(timeout)

    def _await_tunnel(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._alive():
                raise AssertionError(f"port-forward to {self._target} exited")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=2):
                    return
            except OSError:
                time.sleep(0.5)
        raise AssertionError(f"port-forward to {self._target} never accepted a connection")

    def _alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None

    def restart(self, await_trino: bool = True) -> None:
        """Keeps the same local port. Callers already hold it -- the app under test
        built its Trino adapter from it -- so moving it would strand them."""
        self.stop()
        self.start(await_trino=await_trino)

    def ensure_alive(self) -> None:
        """A forward started before the pod was listening exits on the first refused
        connection. Nothing revives it on its own, and a dead tunnel looks exactly
        like a coordinator that never served."""
        if not self._alive():
            self.restart(await_trino=False)

    def _await_coordinator(self, timeout: float) -> None:
        """Ready means Trino answers, not merely that the tunnel is up: a forward to
        a starting coordinator accepts connections and then refuses queries."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._alive():
                # The forward exits on its own if the pod is not accepting yet.
                self.start(timeout=max(timeout - 10, 10))
                return
            try:
                info = httpx.get(f"http://127.0.0.1:{self.port}/v1/info", timeout=5)
                if info.status_code == 200 and not info.json().get("starting", True):
                    return
            except Exception:
                pass
            time.sleep(1)
        raise AssertionError(f"coordinator not serving through the forward on {self.port}")


def restart_coordinator() -> None:
    kubectl("rollout", "restart", COORDINATOR)
    kubectl("rollout", "status", COORDINATOR, "--timeout=600s")


@pytest.fixture(scope="session")
def cluster() -> None:
    if not _cluster_available():
        pytest.skip("no Kubernetes cluster reachable; tier 2 requires kind")


@pytest.fixture(scope="session")
def real_kubernetes(cluster: None) -> RealKubernetes:
    return RealKubernetes()


class ForwardedKubernetes:
    """The real adapter, with validation pods reached through a port-forward.

    In production Apchi runs inside the cluster and talks to a validation pod by its
    pod IP. This test process runs outside, where pod IPs are not routable, so the
    host `pod_state` reports is rewritten to a local forward. It is the same
    accommodation already made to reach the Cluster's own coordinator, and it is the
    only thing adapted: the API server, the pod, its image, the kubelet's states and
    the Trino inside it are all real.
    """

    def __init__(self, real: RealKubernetes) -> None:
        self._real = real
        self._forwards: dict[str, PortForward] = {}
        #: Every pod Apchi asked for, so a test can prove one was really created.
        self.created_pods: list[str] = []

    async def read_secret(self, name: str) -> dict[str, str]:
        return await self._real.read_secret(name)

    async def write_secret(self, name: str, data: dict[str, str]) -> None:
        await self._real.write_secret(name, data)

    async def ready_replicas(self, deployment: str) -> int:
        return await self._real.ready_replicas(deployment)

    async def deployment_image(self, deployment: str, container: str) -> str:
        return await self._real.deployment_image(deployment, container)

    async def create_pod(self, manifest: dict[str, Any]) -> None:
        self.created_pods.append(manifest["metadata"]["name"])
        await self._real.create_pod(manifest)

    async def pod_state(self, name: str) -> PodState:
        state = await self._real.pod_state(name)
        if state.host is None:
            return state
        if name not in self._forwards:
            forward = PortForward(f"pod/{name}", state.port)
            # The pod has an address but Trino may still be starting, which the
            # caller is already polling for; wait only for the tunnel.
            forward.start(await_trino=False)
            self._forwards[name] = forward
        else:
            self._forwards[name].ensure_alive()
        return state.model_copy(update={"host": "127.0.0.1", "port": self._forwards[name].port})

    async def delete_pod(self, name: str) -> None:
        if (forward := self._forwards.pop(name, None)) is not None:
            forward.stop()
        await self._real.delete_pod(name)

    async def delete_pods(self, label_selector: str) -> list[str]:
        names = await self._real.delete_pods(label_selector)
        for name in names:
            if (forward := self._forwards.pop(name, None)) is not None:
                forward.stop()
        return names

    def shutdown(self) -> None:
        for forward in self._forwards.values():
            forward.stop()
        self._forwards.clear()


@pytest.fixture
async def seed_secret(real_kubernetes: RealKubernetes) -> AsyncIterator[None]:
    """Restores the catalog seed Secret, so a test that rewrites it cannot strand the
    shared coordinator with a seed the next test does not expect."""
    original = await real_kubernetes.read_secret(CATALOG_SEED_SECRET)
    try:
        yield
    finally:
        await real_kubernetes.write_secret(CATALOG_SEED_SECRET, original)


@pytest.fixture
def forward(cluster: None) -> Iterator[PortForward]:
    tunnel = PortForward(TRINO_SERVICE, 8080)
    tunnel.start()
    try:
        yield tunnel
    finally:
        tunnel.stop()


@pytest.fixture
def forwarded_kubernetes(real_kubernetes: RealKubernetes) -> ForwardedKubernetes:
    return ForwardedKubernetes(real_kubernetes)


@pytest.fixture
async def e2e_client(
    settings: Settings,
    forwarded_kubernetes: ForwardedKubernetes,
    forward: PortForward,
    seed_secret: None,
) -> AsyncIterator[AsyncClient]:
    """The primary seam with nothing faked behind it.

    Same app, same routes, same pipeline as tier 1 -- the only difference is that
    Kubernetes and Trino are real, so an Apply changes a live cluster.
    """
    kubernetes = forwarded_kubernetes
    app = create_app(settings)
    app.state.kubernetes = kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=forward.port, user=settings.trino_user)
    baseline = await app.state.trino.catalogs()
    try:
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http_client,
            app.router.lifespan_context(app),
        ):
            yield http_client
    finally:
        kubernetes.shutdown()
        for name in await app.state.trino.catalogs() - baseline:
            await app.state.trino.drop_catalog(name)

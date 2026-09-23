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

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.kubernetes import RealKubernetes
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

    def start(self, timeout: float = 120) -> None:
        self._process = subprocess.Popen(
            ["kubectl", "port-forward", self._target, f"{self.port}:{self._remote_port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._await_coordinator(timeout)

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

    def restart(self) -> None:
        """Keeps the same local port. Callers already hold it -- the app under test
        built its Trino adapter from it -- so moving it would strand them."""
        self.stop()
        self.start()

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
async def e2e_client(
    settings: Settings,
    real_kubernetes: RealKubernetes,
    forward: PortForward,
    seed_secret: None,
) -> AsyncIterator[AsyncClient]:
    """The primary seam with nothing faked behind it.

    Same app, same routes, same pipeline as tier 1 -- the only difference is that
    Kubernetes and Trino are real, so an Apply changes a live cluster.
    """
    app = create_app(settings)
    app.state.kubernetes = real_kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=forward.port, user=settings.trino_user)
    baseline = await app.state.trino.catalogs()
    try:
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http_client,
            app.router.lifespan_context(app),
        ):
            yield http_client
    finally:
        for name in await app.state.trino.catalogs() - baseline:
            await app.state.trino.drop_catalog(name)

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
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.kubernetes import PodState, RealKubernetes, RolloutState
from app.adapters.trino import Trino
from app.config import Settings
from app.main import create_app

COORDINATOR = "deploy/trino-coordinator"
TRINO_SERVICE = "svc/trino"
CATALOG_SEED_SECRET = "trino-catalog-seed"
ACCESS_CONTROL_SECRET = "trino-access-control"
EVENT_LISTENER_SECRET = "trino-event-listener"


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

    def _spawn(self) -> None:
        self._process = subprocess.Popen(
            ["kubectl", "port-forward", self._target, f"{self.port}:{self._remote_port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def start(self, timeout: float = 120, await_trino: bool = True) -> None:
        """With await_trino, returns only once Trino is serving through the tunnel.
        Without it, returns as soon as the tunnel accepts a connection -- for a pod
        whose Trino is still starting and whose readiness the caller polls itself."""
        self._spawn()
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

    def serving(self, timeout: float = 2.0) -> bool:
        """Whether Trino answers *through this tunnel*.

        Process liveness is not enough: a forward to a Service keeps running after the pod
        it chose has gone, accepting local connections and failing every stream. That looks
        exactly like a coordinator that died.
        """
        if not self._alive():
            return False
        try:
            response = httpx.get(f"http://127.0.0.1:{self.port}/v1/info", timeout=timeout)
            return response.status_code == 200 and not response.json().get("starting", True)
        except Exception:
            return False

    def _await_coordinator(self, timeout: float) -> None:
        """Ready means Trino answers, not merely that the tunnel is up: a forward to
        a starting coordinator accepts connections and then refuses queries."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._alive():
                # kubectl exits on its own when the pod it chose is not accepting, or when
                # that pod goes away. Respawn and keep checking: returning here on the
                # strength of having respawned would hand back a tunnel nothing has
                # verified, which is how a coordinator that is serving perfectly well ends
                # up refusing Verification's first query.
                self._spawn()
                time.sleep(1)
                continue
            try:
                info = httpx.get(f"http://127.0.0.1:{self.port}/v1/info", timeout=5)
                if info.status_code == 200 and not info.json().get("starting", True):
                    return
            except Exception:
                pass
            time.sleep(1)
        raise AssertionError(f"coordinator not serving through the forward on {self.port}")


def _coordinator_pods() -> int:
    """How many coordinator pods exist, Terminating included."""
    listed = kubectl("get", "pods", "-l", "app=trino,component=coordinator", "--no-headers")
    return len([line for line in listed.splitlines() if line.strip()])


def coordinator_log(lines: int = 400) -> str:
    """The coordinator's own log.

    How Tier 2 proves a listener was adopted. Nothing functional can tell Apchi whether an
    event listener loaded -- its output goes to an external sink -- so the proof is read
    from outside the product rather than turned into a product capability.
    """
    return kubectl("logs", f"deploy/{'trino-coordinator'}", "-c", "trino", f"--tail={lines}")


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

    def __init__(self, real: RealKubernetes, cluster: PortForward | None = None) -> None:
        self._real = real
        #: The tunnel to the Cluster's coordinator. A Rollout kills the pod it is attached
        #: to, so the harness has to revive it -- in production Apchi reaches the
        #: coordinator through its Service, which outlives the pod.
        self._cluster = cluster
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

    async def deployment_pod_spec(self, deployment: str) -> dict[str, Any]:
        return await self._real.deployment_pod_spec(deployment)

    async def create_pod(self, manifest: dict[str, Any]) -> None:
        self.created_pods.append(manifest["metadata"]["name"])
        await self._real.create_pod(manifest)

    async def create_secret(self, name: str, data: dict[str, str], labels: dict[str, str]) -> None:
        await self._real.create_secret(name, data, labels)

    async def delete_secret(self, name: str) -> None:
        await self._real.delete_secret(name)

    async def delete_secrets(self, label_selector: str) -> list[str]:
        return await self._real.delete_secrets(label_selector)

    async def pod_logs(self, name: str, tail: int) -> str:
        return await self._real.pod_logs(name, tail)

    async def restart_deployment(self, deployment: str, reason: str) -> None:
        await self._real.restart_deployment(deployment, reason)

    async def rollout_state(self, deployment: str) -> RolloutState:
        """Revives the Cluster tunnel while polling.

        This is the one call made repeatedly *during* a rollout, so it is where the harness
        notices its own port-forward died with the pod being replaced. Verification runs
        straight afterwards and would otherwise be told the coordinator did not respond --
        which would be the harness's fault, not the Cluster's.
        """
        state = await self._real.rollout_state(deployment)
        if self._cluster is None or not state.complete:
            return state

        unready = state.model_copy(update={"ready": 0, "unavailable": state.replicas})

        # Kubernetes calls the rollout complete while the previous pod is still
        # Terminating, and a Terminating pod still matches the Service selector -- so a
        # port-forward can be attached to one that is about to vanish. Waiting for exactly
        # one coordinator pod is what makes the tunnel's target unambiguous.
        if _coordinator_pods() != 1:
            return unready

        # Rebuilt rather than reused, so the tunnel is established *after* the rollout
        # finished and cannot be holding the pod that was replaced. Verification talks to
        # Trino before it talks to Kubernetes, so there is no later chance to fix this.
        try:
            self._cluster.restart(await_trino=True)
        except AssertionError:
            return unready
        return state

    async def mount_secret_file(
        self, deployment: str, container: str, volume: str, secret: str, path: str, key: str
    ) -> None:
        await self._real.mount_secret_file(deployment, container, volume, secret, path, key)

    async def unmount_secret_file(
        self, deployment: str, container: str, volume: str, path: str
    ) -> None:
        await self._real.unmount_secret_file(deployment, container, volume, path)

    async def pod_state(self, name: str) -> PodState:
        state = await self._real.pod_state(name)
        if state.host is None or state.problem is not None:
            # No point tunnelling to a pod that is already failing -- and a probe that is
            # *meant* to fail, like a listener whose brokers cannot be reached, would take
            # the forward down with it and turn a clean Validation failure into a harness
            # error.
            return state
        if name not in self._forwards:
            forward = PortForward(f"pod/{name}", state.port)
            try:
                # The pod has an address but Trino may still be starting, which the caller
                # is already polling for; wait only for the tunnel.
                forward.start(await_trino=False)
            except AssertionError:
                # The pod went away while the tunnel was being built. Report what Kubernetes
                # said and let the caller poll again; the harness failing to connect is not
                # a verdict about the Candidate.
                forward.stop()
                return state
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
async def cluster_state(
    real_kubernetes: RealKubernetes, forward: PortForward, settings: Settings
) -> AsyncIterator[None]:
    """Puts the shared cluster back after each test: both Secrets Apchi writes, and any
    catalog left behind.

    One fixture owns the whole reset because the order matters. The access-control
    Secret has to go back *before* stray catalogs are dropped: Apply delivers the rules
    as its first step, generated from the Trino identity Apchi is configured with, so a
    test that runs Apchi under a different identity to force a failure also revokes the
    real one's `owner` -- and the cleanup would then be denied its own DROP CATALOG.
    """
    secrets = (CATALOG_SEED_SECRET, ACCESS_CONTROL_SECRET, EVENT_LISTENER_SECRET)
    originals = {name: await real_kubernetes.read_secret(name) for name in secrets}

    def cluster() -> Trino:
        return Trino(host="127.0.0.1", port=forward.port, user=settings.trino_user)

    baseline = await cluster().catalogs()
    try:
        yield
    finally:
        changed_rules = (
            await real_kubernetes.read_secret(ACCESS_CONTROL_SECRET)
            != originals[ACCESS_CONTROL_SECRET]
        )
        for name, content in originals.items():
            await real_kubernetes.write_secret(name, content)
        # Apchi mounts its own volume on the coordinator when a listener is configured.
        # Left behind, the next test inherits a listener it never asked for.
        kubectl(
            "patch",
            "deployment",
            "trino-coordinator",
            "--type=strategic",
            "-p",
            '{"spec":{"template":{"spec":{"volumes":[{"name":"apchi-event-listener",'
            '"$patch":"delete"}],"containers":[{"name":"trino","volumeMounts":'
            '[{"mountPath":"/etc/trino/event-listener.properties","$patch":"delete"}]}]}}}}',
        )

        if changed_rules:
            # Writing the Secret back is not enough. Trino re-reads the rules on its own
            # security.refresh-period, and the kubelet takes up to its syncFrequency to
            # project the change -- the two delays of §7.5, adding up to longer than the
            # next test waits. A restart is slower but certain, and the forward dies with
            # the pod it was attached to.
            restart_coordinator()
            forward.restart()
        for name in await cluster().catalogs() - baseline:
            await cluster().drop_catalog(name)


@pytest.fixture
def forward(cluster: None) -> Iterator[PortForward]:
    tunnel = PortForward(TRINO_SERVICE, 8080)
    tunnel.start()
    try:
        yield tunnel
    finally:
        tunnel.stop()


@pytest.fixture
def forwarded_kubernetes(
    real_kubernetes: RealKubernetes, forward: PortForward
) -> ForwardedKubernetes:
    return ForwardedKubernetes(real_kubernetes, cluster=forward)


@asynccontextmanager
async def running_apchi(
    settings: Settings,
    kubernetes: ForwardedKubernetes,
    forward: PortForward,
) -> AsyncIterator[AsyncClient]:
    """Apchi over a real cluster, with settings of the test's choosing.

    Used where a test needs a second Apchi differing only in configuration -- a
    different Trino identity, a Verification that cannot pass -- against the same
    cluster and the same MongoDB.
    """
    app = create_app(settings)
    app.state.kubernetes = kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=forward.port, user=settings.trino_user)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as client,
        app.router.lifespan_context(app),
    ):
        yield client


@pytest.fixture
async def e2e_client(
    settings: Settings,
    forwarded_kubernetes: ForwardedKubernetes,
    forward: PortForward,
    cluster_state: None,
) -> AsyncIterator[AsyncClient]:
    """The primary seam with nothing faked behind it.

    Same app, same routes, same pipeline as tier 1 -- the only difference is that
    Kubernetes and Trino are real, so an Apply changes a live cluster.
    """
    kubernetes = forwarded_kubernetes
    app = create_app(settings)
    app.state.kubernetes = kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=forward.port, user=settings.trino_user)
    try:
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http_client,
            app.router.lifespan_context(app),
        ):
            yield http_client
    finally:
        # Catalogs are cleaned up by cluster_state, which tears down after this and can
        # do it with the real identity's rules back in force.
        kubernetes.shutdown()

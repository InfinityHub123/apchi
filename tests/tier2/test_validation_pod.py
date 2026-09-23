"""The ephemeral validation coordinator, in a real cluster.

Tier 1 proves what Validation *decides*, with a second container standing in for the
pod. What only a real cluster can prove is that the pod Apchi asks for is a pod
Kubernetes will run and Trino will serve from: the manifest, the image it takes from
the Cluster, and the states the kubelet reports back.

Apchi reaches the pod by its pod IP, which is routable in-cluster and not from here,
so these tests reach the pod the way anything outside a cluster does -- through a
port-forward -- and drive the adapter directly. The decision logic on top of it is
tier 1's, against the same adapter interface.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient

from app.adapters.kubernetes import RealKubernetes
from app.adapters.trino import Trino
from app.config import Settings
from app.pipeline.validation import (
    VALIDATION_SELECTOR,
    pod_name,
    validation_manifest,
)
from tests.tier2.conftest import ForwardedKubernetes, PortForward

pytestmark = pytest.mark.tier2

TRINO_IMAGE = "trinodb/trino:483"


async def _await_host(kubernetes: RealKubernetes, name: str, timeout: float = 300.0) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = await kubernetes.pod_state(name)
        assert state.problem is None, state.problem
        if state.host is not None:
            return state.host
        await asyncio.sleep(2)
    raise AssertionError(f"pod {name} never reported a host")


async def test_the_image_comes_from_the_cluster_not_from_configuration(
    real_kubernetes: RealKubernetes,
) -> None:
    """Configured separately, the validation pod would validate against a version the
    Cluster does not run."""
    settings = Settings()

    image = await real_kubernetes.deployment_image(
        settings.coordinator_deployment_name, settings.trino_container_name
    )

    assert image == TRINO_IMAGE


async def test_the_manifest_produces_a_coordinator_that_serves_and_accepts_ddl(
    real_kubernetes: RealKubernetes,
) -> None:
    """The risky parts of the manifest are the two things it does to a stock image:
    switching on dynamic catalogs through the environment variable the image reads,
    and hiding the image's example catalogs behind an empty volume. If either is
    wrong, every Validation fails for a reason that has nothing to do with the
    Candidate."""
    name = pod_name(f"probe-{uuid.uuid4().hex[:8]}")
    await real_kubernetes.create_pod(validation_manifest(name, TRINO_IMAGE, 600))
    forward = PortForward(f"pod/{name}", 8080)
    try:
        await _await_host(real_kubernetes, name)
        forward.start()
        probe = Trino(host="127.0.0.1", port=forward.port)

        # `memory` is one of the image's example catalogs. Creating a catalog by that
        # name proves the empty volume really hid them.
        await probe.create_catalog("memory", "memory", {})

        assert await probe.catalogs() == {"system", "memory"}
    finally:
        forward.stop()
        await real_kubernetes.delete_pod(name)


async def test_a_pod_that_cannot_start_is_reported_rather_than_waited_out(
    real_kubernetes: RealKubernetes,
) -> None:
    """A real kubelet's ImagePullBackOff, not a fake's. Validation fails on it
    immediately instead of burning its whole timeout."""
    name = pod_name(f"unpullable-{uuid.uuid4().hex[:8]}")
    await real_kubernetes.create_pod(
        validation_manifest(name, "trinodb/trino:no-such-tag-exists", 300)
    )
    try:
        deadline = asyncio.get_running_loop().time() + 180
        problem = None
        while asyncio.get_running_loop().time() < deadline:
            problem = (await real_kubernetes.pod_state(name)).problem
            if problem is not None:
                break
            await asyncio.sleep(2)

        assert problem is not None, "the kubelet never reported a problem"
        assert "ImagePull" in problem or "ErrImagePull" in problem
    finally:
        await real_kubernetes.delete_pod(name)


async def test_orphaned_validation_pods_are_swept_by_their_label(
    real_kubernetes: RealKubernetes,
) -> None:
    """What Apchi does at startup after a crash mid-validation."""
    name = pod_name(f"orphan-{uuid.uuid4().hex[:8]}")
    await real_kubernetes.create_pod(validation_manifest(name, TRINO_IMAGE, 300))

    swept = await real_kubernetes.delete_pods(VALIDATION_SELECTOR)

    assert name in swept
    # Deletion is graceful, so the object outlives the call by a little.
    deadline = asyncio.get_running_loop().time() + 120
    while asyncio.get_running_loop().time() < deadline:
        if (await real_kubernetes.pod_state(name)).phase == "Missing":
            return
        await asyncio.sleep(2)
    raise AssertionError(f"pod {name} was not gone")


async def test_deleting_a_pod_that_is_already_gone_is_not_an_error(
    real_kubernetes: RealKubernetes,
) -> None:
    """Cleanup runs on every path, including the one where the pod never existed."""
    await real_kubernetes.delete_pod(pod_name(f"never-{uuid.uuid4().hex[:8]}"))


async def test_an_apply_validates_against_a_real_pod_and_cleans_it_up(
    e2e_client: AsyncClient,
    forwarded_kubernetes: ForwardedKubernetes,
    real_kubernetes: RealKubernetes,
) -> None:
    """The whole path, in a real cluster: the Apply's Validation stage creates an
    ephemeral coordinator, proves the Candidate against it, and removes it."""
    await e2e_client.post("/api/v1/catalogs", json={"name": "validated", "connector": "memory"})

    started = await e2e_client.post("/api/v1/applies")
    record = await _settled_apply(e2e_client, started.json()["id"])

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert len(forwarded_kubernetes.created_pods) == 1
    pod = forwarded_kubernetes.created_pods[0]
    assert await _gone(real_kubernetes, pod), f"{pod} outlived the Apply"


async def test_a_catalog_trino_rejects_fails_the_apply_in_a_real_cluster(
    e2e_client: AsyncClient, forwarded_kubernetes: ForwardedKubernetes
) -> None:
    """A real pod rejecting the configuration, and a Cluster left untouched."""
    await e2e_client.post(
        "/api/v1/catalogs", json={"name": "imaginary", "connector": "nosuchconnector"}
    )

    started = await e2e_client.post("/api/v1/applies")
    record = await _settled_apply(e2e_client, started.json()["id"])

    assert record["stage"] == "failed"
    assert "imaginary" in record["failure_reason"]
    assert forwarded_kubernetes.created_pods
    assert (await e2e_client.get("/api/v1/snapshots")).json() == []


async def _settled_apply(client: AsyncClient, apply_id: str, timeout: float = 600.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in {"succeeded", "failed"}:
            return record
        await asyncio.sleep(0.5)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


async def _gone(kubernetes: RealKubernetes, pod: str, timeout: float = 180) -> bool:
    """Deletion is graceful, so the object outlives the call that asked for it.

    Polled through the unwrapped adapter: the forwarding wrapper would open a tunnel
    to a pod that is on its way out.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = await kubernetes.pod_state(pod)
        if state.phase == "Missing":
            return True
        await asyncio.sleep(2)
    return False

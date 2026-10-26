"""Proves the real Kubernetes adapter works against a live cluster running
deploy/trino-dev, so tier 1's fake stands in for something that exists.

Tier 2 is not about testing the adapter in isolation -- later tickets drive whole
Applies through this same seam. This ticket only has to prove the harness reaches
a real cluster.
"""

import pytest

from app.adapters.kubernetes import KubernetesAdapter, RealKubernetes
from tests.tier2.conftest import ForwardedKubernetes, PortForward, kubectl

pytestmark = pytest.mark.tier2

CATALOG_SEED_SECRET = "trino-catalog-seed"
WORKER_DEPLOYMENT = "trino-worker"


async def test_reads_the_catalog_seed_secret(real_kubernetes: RealKubernetes) -> None:
    seed = await real_kubernetes.read_secret(CATALOG_SEED_SECRET)

    assert "tpch.properties" in seed


async def test_writes_and_reads_back(real_kubernetes: RealKubernetes, cluster_state: None) -> None:
    """The durable half of the double-write: Apchi writes the Secret, and the catalog
    survives a pod restart because of it."""
    await real_kubernetes.write_secret(
        CATALOG_SEED_SECRET, {"probe.properties": "connector.name=tpch\n"}
    )

    assert (await real_kubernetes.read_secret(CATALOG_SEED_SECRET))["probe.properties"].startswith(
        "connector.name=tpch"
    )


async def test_a_write_removes_keys_it_leaves_out(
    real_kubernetes: RealKubernetes, cluster_state: None
) -> None:
    """The contract the fake cannot prove. A Kubernetes merge patch merges the map
    key by key, so removal needs an explicit null -- and without removal a dropped
    Catalog would be seeded straight back in at the next restart."""
    await real_kubernetes.write_secret(
        CATALOG_SEED_SECRET, {"a.properties": "connector.name=tpch\n"}
    )

    await real_kubernetes.write_secret(
        CATALOG_SEED_SECRET, {"b.properties": "connector.name=tpch\n"}
    )

    assert await real_kubernetes.read_secret(CATALOG_SEED_SECRET) == {
        "b.properties": "connector.name=tpch\n"
    }


async def test_reports_ready_worker_replicas(real_kubernetes: RealKubernetes) -> None:
    """Verification compares this against the count from system.runtime.nodes."""
    assert await real_kubernetes.ready_replicas(WORKER_DEPLOYMENT) >= 1


def test_the_forwarding_wrapper_satisfies_the_adapter_interface(
    real_kubernetes: RealKubernetes,
) -> None:
    """The wrapper delegates method by method, so it is the thing most likely to fall
    behind the adapter. Nothing else catches it: app.state.kubernetes is untyped, so a
    missing method surfaces only as an AttributeError inside a running Apply."""
    assert isinstance(ForwardedKubernetes(real_kubernetes), KubernetesAdapter)


async def test_a_tunnel_that_cannot_be_revived_is_not_a_verdict(
    real_kubernetes: RealKubernetes,
) -> None:
    """A dead port-forward reports the pod's state instead of raising (#174).

    This is the harness failure that looked like a product bug. Tier 2 runs Apchi out of
    cluster, so `pod_state` tunnels to the pod -- and the tests that matter most here build
    a validation probe that is *meant* not to start. The pod dies, the tunnel dies with it,
    and reviving it fails. Unguarded, that `AssertionError` surfaces from `pod_state` in
    place of the `ValidationFailed` that names the Operator's resource, and the failure
    arrives with `resource: None`.

    Provoked deterministically rather than waited for. The real thing is a race between the
    pod dying and Kubernetes reporting it, which is why it took a loaded CI runner to show
    and passed every time in isolation. A cached forward aimed at a pod that was never there
    is the same situation with the timing removed.
    """
    # By name prefix rather than by label: a Deployment names its pods after itself, and the
    # labels differ between deploy/trino-dev and the official chart.
    names = kubectl("get", "pods", "-o", "name").split()
    pod = next(
        (name.removeprefix("pod/") for name in names if name.startswith("pod/trino-coordinator-")),
        "",
    )
    assert pod, f"no coordinator pod among {names}"

    forwarded = ForwardedKubernetes(real_kubernetes)
    doomed = PortForward("pod/apchi-no-such-pod-174", 8080)
    forwarded._forwards[pod] = doomed
    try:
        state = await forwarded.pod_state(pod)
    finally:
        doomed.stop()
        forwarded.shutdown()

    # The pod's own state, reported rather than raised -- and the useless tunnel dropped, so
    # the next poll builds a fresh one rather than failing the same way forever.
    assert state.problem is None, state.problem
    assert pod not in forwarded._forwards

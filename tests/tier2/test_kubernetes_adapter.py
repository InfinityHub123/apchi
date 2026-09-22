"""Proves the real Kubernetes adapter works against a live cluster running
deploy/trino-dev, so tier 1's fake stands in for something that exists.

Tier 2 is not about testing the adapter in isolation -- later tickets drive whole
Applies through this same seam. This ticket only has to prove the harness reaches
a real cluster.
"""

import pytest

from app.adapters.kubernetes import RealKubernetes

pytestmark = pytest.mark.tier2

CATALOG_SEED_SECRET = "trino-catalog-seed"
WORKER_DEPLOYMENT = "trino-worker"


async def test_reads_the_catalog_seed_secret(real_kubernetes: RealKubernetes) -> None:
    seed = await real_kubernetes.read_secret(CATALOG_SEED_SECRET)

    assert "tpch.properties" in seed


async def test_patches_and_reads_back(real_kubernetes: RealKubernetes) -> None:
    """The durable half of the double-write: Apchi patches the Secret, and the
    catalog survives a pod restart because of it."""
    await real_kubernetes.patch_secret(
        CATALOG_SEED_SECRET, {"probe.properties": "connector.name=tpch\n"}
    )

    assert (await real_kubernetes.read_secret(CATALOG_SEED_SECRET))["probe.properties"].startswith(
        "connector.name=tpch"
    )


async def test_reports_ready_worker_replicas(real_kubernetes: RealKubernetes) -> None:
    """Verification compares this against the count from system.runtime.nodes."""
    assert await real_kubernetes.ready_replicas(WORKER_DEPLOYMENT) >= 1

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


async def test_writes_and_reads_back(real_kubernetes: RealKubernetes, seed_secret: None) -> None:
    """The durable half of the double-write: Apchi writes the Secret, and the catalog
    survives a pod restart because of it."""
    await real_kubernetes.write_secret(
        CATALOG_SEED_SECRET, {"probe.properties": "connector.name=tpch\n"}
    )

    assert (await real_kubernetes.read_secret(CATALOG_SEED_SECRET))["probe.properties"].startswith(
        "connector.name=tpch"
    )


async def test_a_write_removes_keys_it_leaves_out(
    real_kubernetes: RealKubernetes, seed_secret: None
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

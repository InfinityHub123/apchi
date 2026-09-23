"""The substitution seam: the fake must satisfy the same interface as the real
adapter, or tier 1 proves nothing about tier 2."""

from app.adapters.kubernetes import KubernetesAdapter, RealKubernetes
from tests.conftest import FakeKubernetes


def test_fake_kubernetes_satisfies_the_adapter_interface() -> None:
    assert isinstance(FakeKubernetes(), KubernetesAdapter)


def test_real_kubernetes_satisfies_the_adapter_interface() -> None:
    """Checked structurally, without constructing it -- that would need a cluster."""
    for method in ("read_secret", "patch_secret", "ready_replicas"):
        assert hasattr(RealKubernetes, method)


async def test_fake_kubernetes_records_patches(fake_kubernetes: FakeKubernetes) -> None:
    await fake_kubernetes.patch_secret("trino-catalog-seed", {"finance.properties": "x"})

    assert await fake_kubernetes.read_secret("trino-catalog-seed") == {"finance.properties": "x"}

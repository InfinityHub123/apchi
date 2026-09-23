"""The substitution seam: the fake must satisfy the same interface as the real
adapter, or tier 1 proves nothing about tier 2."""

from app.adapters.kubernetes import KubernetesAdapter, RealKubernetes
from tests.conftest import FakeKubernetes


def test_fake_kubernetes_satisfies_the_adapter_interface() -> None:
    assert isinstance(FakeKubernetes(), KubernetesAdapter)


def test_real_kubernetes_satisfies_the_adapter_interface() -> None:
    """Constructing it is safe with no cluster: it builds its clients on first use.

    Checked against the Protocol rather than a list of method names, so adding a method
    to the adapter cannot leave a stand-in behind without failing here. A hand-written
    list is how the tier 2 wrapper came to be missing one.
    """
    assert isinstance(RealKubernetes(), KubernetesAdapter)


async def test_fake_kubernetes_records_patches(fake_kubernetes: FakeKubernetes) -> None:
    await fake_kubernetes.write_secret("trino-catalog-seed", {"finance.properties": "x"})

    assert await fake_kubernetes.read_secret("trino-catalog-seed") == {"finance.properties": "x"}

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


def test_the_tier_2_forwarding_wrapper_satisfies_the_adapter_interface() -> None:
    """Checked in tier 1, where it costs nothing and fails fast.

    The wrapper delegates method by method, so it is the thing most likely to fall behind
    the adapter -- and nothing else catches it, because `app.state.kubernetes` is untyped
    and a missing method surfaces only as an AttributeError inside a running Apply, which
    then looks like a failed rollback and engages Maintenance Mode. It lived in tier 2
    until it drifted during a run of a single tier 2 file.
    """
    from tests.tier2.conftest import ForwardedKubernetes

    assert isinstance(ForwardedKubernetes(RealKubernetes()), KubernetesAdapter)

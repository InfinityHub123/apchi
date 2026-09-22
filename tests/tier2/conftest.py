"""Tier 2: the same HTTP API, but against a real Kubernetes cluster.

Skipped unless a cluster is reachable, so the fast suite stays runnable anywhere.
CI provides one via kind.
"""

from collections.abc import Iterator

import pytest

from app.adapters.kubernetes import RealKubernetes


def _cluster_available() -> bool:
    try:
        RealKubernetes()
    except Exception:
        return False
    return True


@pytest.fixture(scope="session")
def real_kubernetes() -> Iterator[RealKubernetes]:
    if not _cluster_available():
        pytest.skip("no Kubernetes cluster reachable; tier 2 requires kind")
    yield RealKubernetes()

"""Proves the tier 1 harness reaches a real Trino, and that the adapter's cluster
membership query works.

Worth asserting rather than assuming: the design originally used /v1/node, which
returns 404 on Trino 483. This is the documented replacement.
"""

from testcontainers.community.trino import TrinoContainer

from app.adapters.trino import Trino


def _adapter(container: TrinoContainer) -> Trino:
    return Trino(
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(8080)),
    )


async def test_adapter_reaches_a_real_trino(trino_cluster: TrinoContainer) -> None:
    assert await _adapter(trino_cluster).is_starting() is False


async def test_adapter_reports_unreachable_as_none() -> None:
    assert await Trino(host="127.0.0.1", port=1).is_starting() is None


async def test_worker_count_comes_from_the_system_table(trino_cluster: TrinoContainer) -> None:
    """A single-node Trino is its own coordinator, so it has no workers. The point
    is that the query runs at all -- /v1/node would 404."""
    assert await _adapter(trino_cluster).active_worker_count() == 0


async def test_catalogs_are_listable(trino_cluster: TrinoContainer) -> None:
    assert "system" in await _adapter(trino_cluster).catalogs()


async def test_validation_target_is_a_separate_cluster(
    trino_cluster: TrinoContainer, trino_validation: TrinoContainer
) -> None:
    """Validation issues CREATE CATALOG against its own Trino. Sharing the Cluster's
    container would create the catalog for real and corrupt the test."""
    assert trino_cluster.get_exposed_port(8080) != trino_validation.get_exposed_port(8080)

"""A connector Trino does not have is refused before it is staged (#170).

`postgresqlaa` used to be accepted: the model constrains the connector to a regex, and
anything outside the seven curated schemas passes through as `supported: false`. The typo
then surfaced from the validation probe, minutes later, in the middle of an Apply.

What makes the check possible without a list in the repository is that **Trino enumerates its
own connectors when asked for one it does not have**, so the authority is the coordinator and
the answer is right for its version and for any custom plugin in its image. The price is that
the answer arrives inside an error message, which no contract covers -- so the test that
matters most here is not the refusal but `test_nothing_is_refused_when_the_cluster_will_not_say`.
"""

from typing import Any

import pytest
from httpx import AsyncClient
from testcontainers.core.container import DockerContainer

from app.adapters.trino import Trino

CATALOGS = "/api/v1/catalogs"


def _catalog(connector: str, name: str = "finance_pg") -> dict[str, Any]:
    return {"name": name, "connector": connector, "properties": {}}


@pytest.fixture
def cluster_trino(trino_cluster: DockerContainer) -> Trino:
    return Trino(
        host=trino_cluster.get_container_host_ip(),
        port=int(trino_cluster.get_exposed_port(8080)),
    )


async def test_the_connector_list_comes_from_the_coordinator(cluster_trino: Trino) -> None:
    """The parse, against a real Trino rather than a recorded string.

    This is the test that fails if Trino rewords the message, which is the whole risk the
    design carries. The three names asserted are the ones no list copied from the
    documentation would have got right: the docs print "Delta Lake", "Google Sheets" and
    "Thrift", and `/usr/lib/trino/plugin` spells the first `delta-lake` with a hyphen.
    """
    factories = await cluster_trino.connector_factories()

    assert factories is not None, "Trino did not name its factories"
    assert {"postgresql", "memory", "delta_lake", "gsheets", "trino_thrift"} <= factories
    assert len(factories) > 20, factories


async def test_reading_the_list_creates_no_catalog(cluster_trino: Trino) -> None:
    """The statement that provokes the list is a `CREATE CATALOG`, so the thing to prove is
    that it does not create one. Trino checks the factory before it writes anything."""
    before = await cluster_trino.catalogs()

    await cluster_trino.connector_factories()

    assert await cluster_trino.catalogs() == before


async def test_the_list_is_read_once(cluster_trino: Trino, monkeypatch: Any) -> None:
    """Cached on the adapter, which is one instance per app. Without this, every
    `POST /catalogs` would issue a failed query against the Cluster."""
    first = await cluster_trino.connector_factories()

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("asked Trino a second time")

    monkeypatch.setattr(cluster_trino, "_read_connector_factories", refuse)

    assert await cluster_trino.connector_factories() == first


async def test_an_unknown_connector_is_refused(applying_client: AsyncClient) -> None:
    response = await applying_client.post(CATALOGS, json=_catalog("postgresqlaa"))

    assert response.status_code == 422, response.text
    body = response.json()
    assert "postgresqlaa" in body["message"]
    assert "Did you mean 'postgresql'?" in body["message"]


async def test_a_refused_connector_stages_nothing(applying_client: AsyncClient) -> None:
    """The refusal happens before the Candidate is touched. A 422 that still staged the
    Catalog would be worse than no check: the next Apply would fail on it."""
    await applying_client.post(CATALOGS, json=_catalog("postgresqlaa"))

    assert (await applying_client.get(CATALOGS)).json() == []


async def test_a_connector_trino_has_is_accepted(applying_client: AsyncClient) -> None:
    response = await applying_client.post(CATALOGS, json=_catalog("memory"))

    assert response.status_code == 201, response.text


async def test_an_uncurated_connector_trino_has_is_still_accepted(
    applying_client: AsyncClient,
) -> None:
    """Uncurated and non-existent are different answers, and this is the one the check must
    not conflate. `blackhole` has no schema in `CURATED_SCHEMAS`, so its properties pass
    through unvalidated -- but it is a real connector and a Catalog may use it."""
    response = await applying_client.post(CATALOGS, json=_catalog("blackhole"))

    assert response.status_code == 201, response.text
    assert response.json()["supported"] is False


async def test_a_patched_connector_is_checked(applying_client: AsyncClient) -> None:
    await applying_client.post(CATALOGS, json=_catalog("memory"))

    response = await applying_client.patch(
        f"{CATALOGS}/finance_pg", json={"connector": "postgresqlaa"}
    )

    assert response.status_code == 422, response.text
    assert "Did you mean 'postgresql'?" in response.json()["message"]


async def test_a_patch_that_omits_the_connector_is_not_checked(
    applying_client: AsyncClient,
) -> None:
    """An edit to the properties of a Catalog whose connector is already staged must not be
    refused for the connector's sake -- including when that connector came from a Trino image
    the Cluster no longer runs."""
    await applying_client.post(CATALOGS, json=_catalog("memory"))

    response = await applying_client.patch(f"{CATALOGS}/finance_pg", json={"properties": {}})

    assert response.status_code == 200, response.text


async def test_nothing_is_refused_when_the_cluster_will_not_say(client: AsyncClient) -> None:
    """The branch that decides whether this feature is safe to ship.

    The `client` fixture points at an unreachable coordinator, so the connector list cannot
    be read. The check must then refuse nothing at all -- the probe at Validation is still
    there to catch the typo, where a check that defaulted to refusing would make every
    Catalog unstageable the moment Trino rewords one sentence or goes briefly unreachable.
    """
    response = await client.post(CATALOGS, json=_catalog("postgresqlaa"))

    assert response.status_code == 201, response.text


async def test_an_unreadable_refusal_names_nothing(cluster_trino: Trino) -> None:
    """Trino refusing for some *other* reason reads as "no opinion", not as an empty list.

    Provoked the way it actually happens: the probe catalog already exists, so Trino answers
    "Catalog ... already exists" and never reaches the factory lookup. An empty list here
    would refuse every connector there is.
    """
    await cluster_trino.create_catalog("apchi_connector_probe", "memory", {})
    try:
        assert await cluster_trino.connector_factories() is None
    finally:
        await cluster_trino.drop_catalog("apchi_connector_probe")

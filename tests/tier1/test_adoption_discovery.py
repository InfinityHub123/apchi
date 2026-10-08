"""The discovery as a document an Admin reads, completes and adopts.

Adoption is not a button. Apchi cannot recover everything -- a Catalog created by DDL has its
properties only in the coordinator's writable store, and Apchi will not invent a connection it
could not read -- so what it could not read is the Admin's to supply, and the document that
asks for it has to survive being worked on over days.

Which makes two things load-bearing: it must touch nothing, and re-reading must not destroy
what somebody typed.
"""

import copy

from httpx import AsyncClient
from testcontainers.core.container import DockerContainer

from app.adapters.trino import Trino
from tests.conftest import FakeKubernetes

BASE = "/api/v1/admin/adoption"
PG = {
    "name": "finance",
    "connector": "postgresql",
    "properties": {
        "connection-url": "jdbc:postgresql://db:5432/finance",
        "connection-user": "reader",
    },
}


def _section(view: dict, name: str) -> dict:
    return next(section for section in view["sections"] if section["section"] == name)


# --- it touches nothing -------------------------------------------------------------


async def test_reading_the_cluster_changes_no_part_of_it(
    client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The property that makes this safe to run against a Cluster that is serving."""
    before = (
        copy.deepcopy(fake_kubernetes.secrets),
        copy.deepcopy(fake_kubernetes.config_maps),
        copy.deepcopy(fake_kubernetes.pod_spec),
    )

    assert (await client.post(f"{BASE}/discoveries")).status_code == 201

    assert (
        copy.deepcopy(fake_kubernetes.secrets),
        copy.deepcopy(fake_kubernetes.config_maps),
        copy.deepcopy(fake_kubernetes.pod_spec),
    ) == before


async def test_nothing_about_a_discovery_reaches_the_candidate(client: AsyncClient) -> None:
    """A discovery is a reading, not a staging. The Candidate is the Operator's and Adoption
    does not write it until an Admin adopts (#91)."""
    await client.post(f"{BASE}/discoveries")
    await client.put(f"{BASE}/discovery/catalogs/finance", json=PG)

    assert (await client.get("/api/v1/catalogs")).json() == []
    assert (await client.get("/api/v1/review")).json()["has_changes"] is False


# --- reading it ---------------------------------------------------------------------


async def test_there_is_no_discovery_until_one_is_read(client: AsyncClient) -> None:
    response = await client.get(f"{BASE}/discovery")

    assert response.status_code == 404
    assert "POST" in response.json()["message"]


async def test_a_discovery_says_where_apchi_looked_and_what_it_guessed(
    client: AsyncClient,
) -> None:
    """An empty result that might be a fact and might be a wrong turn is not useful."""
    view = (await client.post(f"{BASE}/discoveries")).json()

    mapping = _section(view, "certificate_mapping")
    assert mapping["looked_at"] == ["/etc/trino/user-mapping.json"]
    assert "where Apchi would put one" in mapping["guessed_because"]


async def test_every_problem_says_where_it_is(client: AsyncClient) -> None:
    """Addressable individually: an Admin has to see which Catalog is incomplete and what it
    needs, not that something somewhere is wrong."""
    view = (await client.post(f"{BASE}/discoveries")).json()

    assert view["outstanding"]
    assert all("kind" in problem and "detail" in problem for problem in view["outstanding"])


# --- supplying what Apchi could not read --------------------------------------------


async def test_an_amendment_is_judged_the_way_staging_the_same_catalog_is(
    client: AsyncClient,
) -> None:
    """Through the same code, so "validated the way staging is" is true rather than
    approximately true. Same error shape as POST /catalogs, down to the suggestion."""
    await client.post(f"{BASE}/discoveries")

    response = await client.put(
        f"{BASE}/discovery/catalogs/finance",
        json={**PG, "properties": {"connection-uri": "jdbc:postgresql://db/f"}},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "unprocessable_payload"
    assert any("did you mean 'connection-url'" in d["problem"] for d in body["details"])


async def test_an_amendment_persists_and_shows_as_supplied(client: AsyncClient) -> None:
    await client.post(f"{BASE}/discoveries")
    await client.put(f"{BASE}/discovery/catalogs/finance", json=PG)

    view = (await client.get(f"{BASE}/discovery")).json()
    catalogs = _section(view, "catalogs")

    assert catalogs["amended"] == ["finance"]
    assert catalogs["resources"]["finance"]["properties"]["connection-user"] == "reader"


async def test_a_body_naming_a_different_catalog_is_refused(client: AsyncClient) -> None:
    """An amendment answers Apchi's question about one named Catalog, so a mismatch is a
    mistake worth refusing rather than quietly resolving."""
    await client.post(f"{BASE}/discoveries")

    response = await client.put(f"{BASE}/discovery/catalogs/warehouse", json=PG)

    assert response.status_code == 409
    assert "'finance'" in response.json()["message"]


async def test_an_amendment_can_be_withdrawn(client: AsyncClient) -> None:
    await client.post(f"{BASE}/discoveries")
    await client.put(f"{BASE}/discovery/catalogs/finance", json=PG)

    view = (await client.delete(f"{BASE}/discovery/catalogs/finance")).json()

    assert _section(view, "catalogs")["amended"] == []


async def test_withdrawing_something_never_supplied_is_not_found(client: AsyncClient) -> None:
    await client.post(f"{BASE}/discoveries")

    response = await client.delete(f"{BASE}/discovery/catalogs/nothing")

    assert response.status_code == 404


async def test_amending_before_reading_is_not_found(client: AsyncClient) -> None:
    response = await client.put(f"{BASE}/discovery/catalogs/finance", json=PG)

    assert response.status_code == 404


# --- re-reading -----------------------------------------------------------------------


async def test_re_reading_keeps_what_was_supplied(client: AsyncClient) -> None:
    """The reason the store keeps readings and amendments apart. An Admin re-reads precisely
    when they are unsure, which is the worst moment to take their work away."""
    await client.post(f"{BASE}/discoveries")
    await client.put(f"{BASE}/discovery/catalogs/finance", json=PG)

    view = (await client.post(f"{BASE}/discoveries")).json()

    assert _section(view, "catalogs")["amended"] == ["finance"]


async def test_re_reading_says_what_the_cluster_changed(
    client: AsyncClient, fake_kubernetes: FakeKubernetes, settings
) -> None:
    """Said rather than silently merged. Somebody who supplied a catalog's properties on
    Monday should hear that the Cluster moved before they adopt it on Thursday."""
    fake_kubernetes.secrets[settings.certificate_mapping_secret_name] = {
        "user-mapping.json": '{"rules": [{"pattern": "CN=(.*?),.*"}]}'
    }
    await client.post(f"{BASE}/discoveries")

    fake_kubernetes.secrets[settings.certificate_mapping_secret_name] = {
        "user-mapping.json": '{"rules": [{"pattern": "^(.*)@example[.]com$"}]}'
    }
    view = (await client.post(f"{BASE}/discoveries")).json()

    assert view["changed_since_last_read"] == [
        "certificate_mapping: pattern has changed on the Cluster"
    ]


async def test_a_first_reading_reports_no_changes(client: AsyncClient) -> None:
    view = (await client.post(f"{BASE}/discoveries")).json()

    assert view["changed_since_last_read"] == []


# --- completeness ---------------------------------------------------------------------


async def test_a_discovery_with_something_outstanding_is_not_complete(
    client: AsyncClient,
) -> None:
    """What #101 and #91 gate on: nothing half-understood reaches the Cluster."""
    view = (await client.post(f"{BASE}/discoveries")).json()

    assert view["complete"] is False
    assert view["outstanding"]


async def test_a_cutover_requirement_does_not_keep_a_discovery_incomplete(
    client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """It names something the deployment must change *after* adoption. Counting it would make
    a Cluster impossible to adopt until it had been changed in a way that loses its catalogs
    (§82's amendment)."""
    fake_kubernetes.pod_spec["initContainers"] = []

    view = (await client.post(f"{BASE}/discoveries")).json()

    assert "cutover" not in {problem["kind"] for problem in view["outstanding"]}


async def test_discarding_removes_the_discovery_and_the_amendments(
    client: AsyncClient,
) -> None:
    """Nothing on the Cluster is touched -- a discovery never reached it."""
    await client.post(f"{BASE}/discoveries")
    await client.put(f"{BASE}/discovery/catalogs/finance", json=PG)

    assert (await client.delete(f"{BASE}/discovery")).status_code == 204

    assert (await client.get(f"{BASE}/discovery")).status_code == 404


# --- against a real Trino ---------------------------------------------------------------


async def test_supplying_an_incomplete_catalogs_properties_resolves_it(
    applying_client: AsyncClient, trino_cluster: DockerContainer
) -> None:
    """The case the whole document exists for, against a Trino really serving a catalog whose
    properties live only in its own store.

    The catalog is created by DDL straight against the Cluster, deliberately going round
    Apchi: that is how such a catalog comes to exist on a real Cluster, and it is the only way
    to produce one Apchi can name and cannot reconstruct. Apchi will not invent its
    properties, so supplying them is what turns an unanswerable discovery into one that can
    be adopted.
    """
    cluster = Trino(
        host=trino_cluster.get_container_host_ip(),
        port=int(trino_cluster.get_exposed_port(8080)),
    )
    await cluster.create_catalog("legacy", "tpch", {})

    view = (await applying_client.post(f"{BASE}/discoveries")).json()
    incomplete = [p for p in view["outstanding"] if p["kind"] == "incomplete"]

    assert [p["path"] for p in incomplete] == ["legacy"]
    assert "will not invent" in incomplete[0]["detail"]

    supplied = await applying_client.put(
        f"{BASE}/discovery/catalogs/legacy",
        json={"name": "legacy", "connector": "tpch", "properties": {}},
    )

    assert supplied.status_code == 200
    body = supplied.json()
    assert [p for p in body["outstanding"] if p["kind"] == "incomplete"] == []
    assert _section(body, "catalogs")["resources"]["legacy"]["connector"] == "tpch"

    # Cleaned up, because the Cluster container is shared with every other tier 1 test and a
    # catalog left behind would make the next discovery find something it did not create.
    await cluster.drop_catalog("legacy")

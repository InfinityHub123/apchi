"""A Catalog that presents a Client Certificate.

Two ways to name one, because Trino has no general property for it: a `certificate` field
for the connectors Apchi knows how to wire, and `${cert:name}` / `${key:name}` tokens
anywhere else. Either way the Operator never types a path, and either way the reference is
what Apchi validates.

The reference is checked over the whole Candidate rather than at request time, so a Catalog
can be written before the certificate it will use is uploaded.
"""

import asyncio

from httpx import AsyncClient

from app.pipeline.applies import TERMINAL
from app.sections.client_certificates.generator import MOUNT_DIR
from tests.certificates import bundle
from tests.conftest import FakeKubernetes

PG = "jdbc:postgresql://db:5432/finance?sslmode=verify-full"
WIRED = {
    "name": "finance",
    "connector": "postgresql",
    "properties": {"connection-url": PG},
    "certificate": "finance",
}
#: The escape hatch: the same connector, wired by hand. What an Operator does when Apchi
#: does not know their connector's convention -- here, on one it does, so the test is about
#: the tokens rather than about the connector.
TOKENS = {
    "name": "manual",
    "connector": "postgresql",
    "properties": {
        "connection-url": (
            "jdbc:postgresql://db:5432/manual?sslmode=verify-full"
            "&sslcert=${cert:finance}&sslkey=${key:finance}"
        )
    },
}


async def _upload(client: AsyncClient, name: str = "finance") -> None:
    uploaded = await client.post(
        "/api/v1/certificates",
        data={"name": name},
        files={"archive": (f"{name}.zip", bundle(), "application/zip")},
    )
    assert uploaded.status_code == 201, uploaded.json()


async def _validation(client: AsyncClient, timeout: float = 90.0) -> dict:
    started = await client.post("/api/v1/validations")
    assert started.status_code == 202
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/validations/{started.json()['id']}")).json()
        if record["outcome"] != "running":
            return record
        await asyncio.sleep(0.05)
    raise AssertionError("Validation never finished")


async def _apply(client: AsyncClient, timeout: float = 180.0) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError("Apply never settled")


async def test_a_catalog_may_be_written_before_its_certificate_exists(
    client: AsyncClient,
) -> None:
    """Refused at request time, an Operator could not write the Catalog first."""
    accepted = await client.post("/api/v1/catalogs", json=WIRED)

    assert accepted.status_code == 201


async def test_a_reference_to_a_certificate_nobody_staged_fails_validation(
    applying_client: AsyncClient,
) -> None:
    await applying_client.post("/api/v1/catalogs", json=WIRED)

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "failed"
    failure = verdict["failures"][0]
    assert failure["section"] == "catalogs"
    assert failure["resource"] == "finance"
    assert "'finance'" in failure["reason"]


async def test_a_token_naming_a_certificate_nobody_staged_fails_validation(
    applying_client: AsyncClient,
) -> None:
    """The escape hatch is validated like the field: a path Apchi expands is a reference."""
    await applying_client.post("/api/v1/catalogs", json=TOKENS)

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "failed"
    assert verdict["failures"][0]["resource"] == "manual"


async def test_a_reference_that_is_staged_passes_validation(
    applying_client: AsyncClient,
) -> None:
    await _upload(applying_client)
    await applying_client.post("/api/v1/catalogs", json=WIRED)

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "passed", verdict["failures"]


async def test_the_certificate_paths_reach_both_copies_of_the_catalog(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The DDL and the durable Secret have to say the same thing, or the catalog works now
    and comes back different at the next pod start."""
    await _upload(applying_client)
    await applying_client.post("/api/v1/catalogs", json=WIRED)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    seeded = fake_kubernetes.secrets["trino-catalog-seed"]["finance.properties"]
    assert f"sslcert={MOUNT_DIR}/finance.crt" in seeded
    assert f"sslkey={MOUNT_DIR}/finance.key" in seeded
    assert "sslmode=verify-full" in seeded, "what the Operator chose is still there"


async def test_a_token_is_expanded_in_whatever_property_it_appears_in(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await _upload(applying_client)
    await applying_client.post("/api/v1/catalogs", json=TOKENS)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    seeded = fake_kubernetes.secrets["trino-catalog-seed"]["manual.properties"]
    assert f"sslcert={MOUNT_DIR}/finance.crt" in seeded
    assert f"sslkey={MOUNT_DIR}/finance.key" in seeded


async def test_removing_a_certificate_a_catalog_presents_is_refused(
    client: AsyncClient,
) -> None:
    """Otherwise the Catalog is left pointing at a file that is about to disappear, and the
    failure surfaces as a connection error with nothing pointing back at the deletion."""
    await _upload(client)
    await client.post("/api/v1/catalogs", json=WIRED)

    refused = await client.delete("/api/v1/certificates/finance")

    assert refused.status_code == 409
    assert "finance" in refused.json()["message"]


async def test_a_certificate_no_catalog_uses_can_be_removed(client: AsyncClient) -> None:
    await _upload(client)
    await _upload(client, "spare")
    await client.post("/api/v1/catalogs", json=WIRED)

    removed = await client.delete("/api/v1/certificates/spare")

    assert removed.status_code == 204


async def test_the_certificate_is_not_what_an_operator_typed_into_properties(
    client: AsyncClient,
) -> None:
    """What is stored is what the Operator wrote. The path appears only in what Trino gets,
    so changing where Apchi mounts certificates does not rewrite anybody's Candidate."""
    await _upload(client)
    await client.post("/api/v1/catalogs", json=WIRED)

    staged = (await client.get("/api/v1/catalogs/finance")).json()

    assert staged["properties"]["connection-url"] == PG
    assert staged["certificate"] == "finance"

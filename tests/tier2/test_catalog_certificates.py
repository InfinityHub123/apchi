"""A Catalog that presents a Client Certificate, against a real cluster.

The fact this whole ticket turned on is here: `CREATE CATALOG` does not open a connection,
so a catalog whose `sslcert` names a file that is not there is created quite happily. That is
why Apply does not retry the DDL to find out whether the certificate arrived -- there is
nothing in the DDL to find out from.

What tier 2 can prove is the rest: the paths Apchi renders reach a real coordinator, the
catalog is created with them, and the certificate is in the directory beside it.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.pipeline.applies import TERMINAL
from app.sections.client_certificates.generator import MOUNT_DIR
from tests.certificates import bundle
from tests.tier2.conftest import file_appears

pytestmark = pytest.mark.tier2

CATALOG = {
    "name": "certcat",
    "connector": "postgresql",
    "properties": {"connection-url": "jdbc:postgresql://nowhere.invalid:5432/db?sslmode=require"},
    "certificate": "finance",
}


async def _apply(client: AsyncClient, timeout: float = 900.0) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(1)
    raise AssertionError("Apply never settled")


async def test_a_catalog_and_the_certificate_it_presents_apply_together(
    e2e_client: AsyncClient, real_kubernetes
) -> None:
    """One Apply carries both, and neither needs the coordinator restarted."""
    before = await real_kubernetes.rollout_state("trino-coordinator")
    uploaded = await e2e_client.post(
        "/api/v1/certificates",
        data={"name": "finance"},
        files={"archive": ("finance.zip", bundle(), "application/zip")},
    )
    assert uploaded.status_code == 201, uploaded.json()
    created = await e2e_client.post("/api/v1/catalogs", json=CATALOG)
    assert created.status_code == 201, created.json()

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    after = await real_kubernetes.rollout_state("trino-coordinator")
    assert after.generation == before.generation, "neither a catalog nor a certificate restarts"

    # The catalog really exists on the coordinator, with the rendered paths in it.
    seeded = await real_kubernetes.read_secret("trino-catalog-seed")
    assert f"sslcert={MOUNT_DIR}/finance.crt" in seeded["certcat.properties"]
    assert f"sslkey={MOUNT_DIR}/finance.key" in seeded["certcat.properties"]
    # Polled, not asserted outright: Apply does not wait for the kubelet, and says so.
    assert await file_appears(MOUNT_DIR, "finance.crt")


async def test_a_catalog_referencing_an_absent_certificate_never_reaches_the_cluster(
    e2e_client: AsyncClient, real_kubernetes
) -> None:
    """Trino would accept this catalog -- it does not read the file until a query runs --
    so Validation is the only thing standing between an Operator's typo and a catalog that
    fails at its first query."""
    await e2e_client.post("/api/v1/catalogs", json=CATALOG)

    record = await _apply(e2e_client)

    assert record["stage"] == "failed"
    assert "finance" in (record["failure_reason"] or "")
    assert "certcat" not in await real_kubernetes.read_secret("trino-catalog-seed")

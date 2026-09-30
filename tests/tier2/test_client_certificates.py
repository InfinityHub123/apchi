"""Client Certificates against a real cluster.

One thing matters here and tier 1 cannot show it: the file has to *appear*, in a directory
that is already mounted, on a pod that is not replaced. That is the whole of ADR-0005, and
it is the difference between uploading a certificate and restarting the Cluster because
somebody uploaded a certificate.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.pipeline.applies import TERMINAL
from app.sections.client_certificates.generator import MOUNT_DIR
from tests.certificates import bundle
from tests.tier2.conftest import file_appears, kubectl

pytestmark = pytest.mark.tier2

#: The kubelet projects a Secret change within seconds normally, bounded by its
#: syncFrequency of a minute -- the bound to design against, so the bound to wait for.
_PROJECTED_WITHIN = 150.0


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


async def test_a_certificate_appears_on_a_pod_that_is_never_replaced(
    e2e_client: AsyncClient, real_kubernetes
) -> None:
    """The whole point of one Secret mounted once: the pod spec does not change, so no
    rollout, so no query dies for an upload."""
    before = await real_kubernetes.rollout_state("trino-coordinator")
    uploaded = await e2e_client.post(
        "/api/v1/certificates",
        data={"name": "finance"},
        files={"archive": ("finance.zip", bundle(), "application/zip")},
    )
    assert uploaded.status_code == 201, uploaded.json()

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    after = await real_kubernetes.rollout_state("trino-coordinator")
    assert after.generation == before.generation, "the pod template was never patched"
    assert await file_appears(MOUNT_DIR, "finance.crt"), (
        "the certificate never reached the coordinator"
    )
    assert await file_appears(MOUNT_DIR, "finance.key")


async def test_a_certificate_reaches_the_workers_too(e2e_client: AsyncClient) -> None:
    """Workers open their own connections to data sources, so a certificate only the
    coordinator can read is a catalog that works for metadata and fails for data."""
    await e2e_client.post(
        "/api/v1/certificates",
        data={"name": "finance"},
        files={"archive": ("finance.zip", bundle(), "application/zip")},
    )

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    assert await file_appears(MOUNT_DIR, "finance.crt", "deploy/trino-worker")


async def test_removing_a_certificate_takes_the_files_away(e2e_client: AsyncClient) -> None:
    """A file left behind is key material outliving its use, in a directory every process in
    the pod can read."""
    await e2e_client.post(
        "/api/v1/certificates",
        data={"name": "finance"},
        files={"archive": ("finance.zip", bundle(), "application/zip")},
    )
    await _apply(e2e_client)
    assert await file_appears(MOUNT_DIR, "finance.crt")

    await e2e_client.delete("/api/v1/certificates/finance")
    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    deadline = asyncio.get_running_loop().time() + _PROJECTED_WITHIN
    while asyncio.get_running_loop().time() < deadline:
        if (
            "finance.crt"
            not in kubectl(
                "exec", "deploy/trino-coordinator", "-c", "trino", "--", "ls", MOUNT_DIR
            ).split()
        ):
            return
        await asyncio.sleep(3)
    raise AssertionError("the certificate is still on the coordinator")

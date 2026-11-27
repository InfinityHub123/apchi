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
from tests.tier2.conftest import file_appears, kubectl, pod_template

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


async def _gone(deployment: str, name: str) -> bool:
    deadline = asyncio.get_running_loop().time() + _PROJECTED_WITHIN
    while asyncio.get_running_loop().time() < deadline:
        listed = kubectl("exec", deployment, "-c", "trino", "--", "ls", MOUNT_DIR).split()
        if name not in listed:
            return True
        await asyncio.sleep(3)
    return False


async def test_a_certificate_comes_and_goes_without_replacing_a_pod(
    e2e_client: AsyncClient,
) -> None:
    """Uploaded, then removed, on both roles. One scenario, because each half waits out the
    kubelet's projection and that wait is the cost of every test here (#300).

    - **It appears on a pod that is never replaced.** The whole point of one Secret mounted
      once: the pod spec does not change, so no rollout, so no query dies for an upload.
    - **It reaches the workers too.** Workers open their own connections to data sources, so
      a certificate only the coordinator can read is a catalog that works for metadata and
      fails for data.
    - **Removing it takes the files away.** A file left behind is key material outliving its
      use, in a directory every process in the pod can read.
    """
    before = pod_template()
    uploaded = await e2e_client.post(
        "/api/v1/certificates",
        data={"name": "finance"},
        files={"archive": ("finance.zip", bundle(), "application/zip")},
    )
    assert uploaded.status_code == 201, uploaded.json()

    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    assert pod_template() == before, "the pod template was never patched"
    coordinator, key, worker = await asyncio.gather(
        file_appears(MOUNT_DIR, "finance.crt"),
        file_appears(MOUNT_DIR, "finance.key"),
        file_appears(MOUNT_DIR, "finance.crt", "deploy/trino-worker"),
    )
    assert coordinator, "the certificate never reached the coordinator"
    assert key
    assert worker, "the certificate never reached the workers"

    await e2e_client.delete("/api/v1/certificates/finance")
    record = await _apply(e2e_client)

    assert record["stage"] == "succeeded", record
    gone = await asyncio.gather(
        _gone("deploy/trino-coordinator", "finance.crt"),
        _gone("deploy/trino-worker", "finance.crt"),
    )
    assert gone == [True, True], "the certificate is still on a pod"

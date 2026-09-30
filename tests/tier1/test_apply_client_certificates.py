"""Applying Client Certificates.

Delivery is the whole of it: the files land in a Secret the Admin already mounts, so nothing
about the pod changes and no Rollout is needed. What the tests here pin is that the Secret
holds exactly what is staged -- a certificate removed from the Candidate has to leave the
durable copy too, or it reappears at the next pod start.
"""

import asyncio

from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.config import Settings
from app.main import create_app
from app.pipeline.applies import TERMINAL
from app.sections.client_certificates.generator import MOUNT_DIR
from tests.certificates import bundle
from tests.conftest import FakeKubernetes

SECRET = "trino-client-certificates"


async def _upload(client: AsyncClient, name: str, archive: bytes | None = None) -> object:
    return await client.post(
        "/api/v1/certificates",
        data={"name": name},
        files={"archive": (f"{name}.zip", archive or bundle(), "application/zip")},
    )


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


async def test_applying_delivers_the_pair_and_restarts_nothing(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await _upload(applying_client, "finance")

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    assert set(fake_kubernetes.secrets[SECRET]) == {"finance.crt", "finance.key"}
    assert "BEGIN CERTIFICATE" in fake_kubernetes.secrets[SECRET]["finance.crt"]
    assert "BEGIN PRIVATE KEY" in fake_kubernetes.secrets[SECRET]["finance.key"]
    assert fake_kubernetes.restarts == [], "nobody's query dies for an upload"
    assert MOUNT_DIR not in fake_kubernetes.mounts, "the Admin owns this mount"


async def test_a_removed_certificate_leaves_the_secret(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Otherwise it is still in the durable copy, and a pod restart brings back key material
    the Operator deleted."""
    await _upload(applying_client, "finance")
    await _upload(applying_client, "staging")
    await _apply(applying_client)
    await applying_client.delete("/api/v1/certificates/finance")

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    assert set(fake_kubernetes.secrets[SECRET]) == {"staging.crt", "staging.key"}


async def test_applying_a_renewal_replaces_the_files_under_the_same_name(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await _upload(applying_client, "finance")
    await _apply(applying_client)
    before = fake_kubernetes.secrets[SECRET]["finance.crt"]

    await _upload(applying_client, "finance")
    await _apply(applying_client)

    assert fake_kubernetes.secrets[SECRET]["finance.crt"] != before
    assert set(fake_kubernetes.secrets[SECRET]) == {"finance.crt", "finance.key"}


async def test_reverting_the_certificates_puts_the_key_material_back(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """A Section Revert rewrites the whole Secret, which is only possible because the
    Candidate holds the key material rather than only its metadata (ADR-0005)."""
    await _upload(applying_client, "finance")
    await _apply(applying_client)
    await applying_client.delete("/api/v1/certificates/finance")
    await _apply(applying_client)

    effect = (
        await applying_client.post("/api/v1/certificates/revert", json={"snapshot": 1})
    ).json()
    record = await _apply(applying_client)

    assert effect["sections"] == ["client_certificates"]
    assert effect["cost"]["restarts_coordinator"] is False
    assert record["stage"] == "succeeded", record
    assert set(fake_kubernetes.secrets[SECRET]) == {"finance.crt", "finance.key"}


async def test_an_upload_that_would_outgrow_the_secret_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Refused with that reason rather than as a rejected write from the API server. The
    ceiling is a setting so this can be proved without uploading a megabyte."""
    tiny = settings.model_copy(update={"client_certificate_max_bytes": 1024})
    app = create_app(tiny)
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=1)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as client,
        app.router.lifespan_context(app),
    ):
        refused = await _upload(client, "finance")

    assert refused.status_code == 422
    assert "Kubernetes Secret may hold" in refused.json()["message"]

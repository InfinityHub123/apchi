"""What Trino was started from, read off its Deployments rather than MongoDB (#290).

A restart writes the checksum of the files the new pods start from onto the pod template, and
Apchi compares it with the checksum of what it would render now. These prove the decisions
that rest on it against the fake: which restarts it causes, which it prevents, and that the
Snapshot a Cluster was verified on is recorded on the Deployment. That Kubernetes copies the
annotation onto real pods, and that writing the Deployment's own restarts nothing, is tier 2's.
"""

import asyncio
from typing import Any

from httpx import AsyncClient

from app.adapters.kubernetes import PREFIX
from app.pipeline.applies import TERMINAL
from app.pipeline.checksum import VERIFIED_SNAPSHOT
from app.pipeline.validation import ROLE_LABEL
from tests.conftest import FakeKubernetes

COORDINATOR = "trino-coordinator"
WORKER = "trino-worker"
OPERATOR_APPLIES = "/api/v1/applies"
ADMIN_APPLIES = "/api/v1/admin/applies"
MEMORY = {"name": "scratch", "connector": "memory", "properties": {}}
GLOBAL = {"path": "global", "hard_concurrency_limit": 100, "max_queued": 1000}


async def _apply(
    http: AsyncClient, path: str = ADMIN_APPLIES, timeout: float = 180.0
) -> dict[str, Any]:
    started = await http.post(path, json={})
    assert started.status_code == 202, started.text
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await http.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return dict(record)
        await asyncio.sleep(0.05)
    raise AssertionError("the Apply never settled")


async def _worker_change(http: AsyncClient, concurrency: str = "16") -> None:
    response = await http.put(
        "/api/v1/admin/server-configuration",
        json={"worker": {"config.properties": {"task.concurrency": concurrency}}},
    )
    assert response.status_code == 200, response.text


def test_every_key_apchi_writes_is_under_one_prefix() -> None:
    assert PREFIX == "apchi/"
    assert ROLE_LABEL.startswith(PREFIX)
    assert VERIFIED_SNAPSHOT.startswith(PREFIX)


async def test_a_restart_writes_a_checksum_the_next_apply_accepts(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await _worker_change(applying_client)
    await _apply(applying_client)
    written = fake_kubernetes.checksums[WORKER]

    await _apply(applying_client)

    assert len(written) == 64
    assert fake_kubernetes.restarted == [WORKER]
    assert fake_kubernetes.checksums == {WORKER: written}


async def test_pods_started_from_older_files_are_restarted_though_the_secrets_match(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """An Apply stopped between writing the Secret and the restart leaves exactly this: the
    Secret is new, and the pods still carry the checksum of the files before it."""
    await _worker_change(applying_client)
    await _apply(applying_client)
    current = fake_kubernetes.checksums[WORKER]
    fake_kubernetes.checksums[WORKER] = "0" * 64

    review = (await applying_client.get("/api/v1/review")).json()
    record = await _apply(applying_client, OPERATOR_APPLIES)

    assert record["stage"] == "succeeded", record
    assert review["cost"]["restarts_workers"] is True
    assert review["cost"]["restarts_coordinator"] is False
    assert fake_kubernetes.restarted == [WORKER, WORKER]
    assert "the files the worker started from" in fake_kubernetes.restarts[-1]
    assert fake_kubernetes.checksums[WORKER] == current


async def test_a_deployment_apchi_never_restarted_is_judged_by_its_secrets(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """A fresh install's pods started after Apchi wrote their Secrets, so a Deployment with no
    checksum is no reason to end every running query."""
    await _apply(applying_client, OPERATOR_APPLIES)

    assert fake_kubernetes.restarted == []
    assert fake_kubernetes.checksums == {}


async def test_startup_files_move_the_coordinators_checksum_and_a_catalog_does_not(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    raw = await applying_client.put(
        "/api/v1/admin/files/etc/trino/session-property-config.properties",
        json={"content": "session-property-config.configuration-manager=file\n"},
    )
    assert raw.status_code == 200, raw.text
    await _apply(applying_client)
    after_raw_file = fake_kubernetes.checksums[COORDINATOR]

    await applying_client.post("/api/v1/resource-groups", json=GLOBAL)
    await _apply(applying_client, OPERATOR_APPLIES)
    after_resource_groups = fake_kubernetes.checksums[COORDINATOR]

    await applying_client.post("/api/v1/catalogs", json=MEMORY)
    await _apply(applying_client, OPERATOR_APPLIES)

    assert after_resource_groups != after_raw_file
    assert fake_kubernetes.restarted == [COORDINATOR, COORDINATOR]
    assert fake_kubernetes.checksums[COORDINATOR] == after_resource_groups


async def test_the_verified_snapshot_is_written_on_both_deployments(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/catalogs", json=MEMORY)

    record = await _apply(applying_client, OPERATOR_APPLIES)

    assert record["snapshot"] == 1, record
    for deployment in (COORDINATOR, WORKER):
        assert fake_kubernetes.deployment_annotations[deployment] == {VERIFIED_SNAPSHOT: "1"}


async def test_an_admin_apply_records_the_snapshot_it_delivered(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/catalogs", json=MEMORY)
    await _apply(applying_client, OPERATOR_APPLIES)
    await _worker_change(applying_client)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    assert fake_kubernetes.deployment_annotations[WORKER] == {VERIFIED_SNAPSHOT: "1"}


async def test_a_failed_apply_leaves_the_verified_snapshot_where_it_was(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/catalogs", json=MEMORY)
    await _apply(applying_client, OPERATOR_APPLIES)
    await applying_client.post("/api/v1/resource-groups", json=GLOBAL)
    fake_kubernetes.report_a_missing_worker_once = True

    record = await _apply(applying_client, OPERATOR_APPLIES)

    assert record["stage"] == "failed", record
    assert fake_kubernetes.deployment_annotations[COORDINATOR] == {VERIFIED_SNAPSHOT: "1"}


async def test_a_rollback_restarts_the_pods_onto_the_restored_files(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The rollback writes the checksum of what it restored, so the next Apply finds the pods
    on the files it would deliver and restarts nothing."""
    await _worker_change(applying_client, "16")
    await _apply(applying_client)
    good = fake_kubernetes.checksums[WORKER]
    await _worker_change(applying_client, "2")
    fake_kubernetes.report_a_missing_worker_once = True
    assert (await _apply(applying_client))["stage"] == "failed"

    restarts = len(fake_kubernetes.restarted)
    await _apply(applying_client)

    assert fake_kubernetes.checksums[WORKER] == good
    assert len(fake_kubernetes.restarted) == restarts

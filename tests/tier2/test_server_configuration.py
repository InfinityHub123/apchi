"""Trino's own files on a real coordinator (§14, ADR-0010).

Tier 1 proves the mechanics against a fake. What only a real cluster can prove is that Trino
reads what Apchi renders, and refuses what it should:

* an Admin's property reaches Trino, and sizing reaches it from the pod rather than a file
* a misspelled property, an invalid value or log level is refused **on the probe**, and the
  Cluster is not touched
* a property the probe accepts but the Cluster cannot run with is rolled back, properties and
  file together, and the coordinator comes back serving
* the coordinator's Secret is written on Apchi's first start and never rewritten after
* nothing but Apchi may be mounted under /etc/trino
"""

import asyncio
import json
import subprocess
from typing import Any

import pytest
from httpx import AsyncClient

from app.adapters.trino import Trino
from app.config import Settings
from app.sections.admin import AdminValues
from app.sections.server_configuration import coordinator_secret
from tests.tier2.conftest import (
    COORDINATOR,
    SERVER_CONFIG_SECRET,
    WORKER,
    ForwardedKubernetes,
    PortForward,
    _live_secret,
    coordinator_log,
    kubectl,
    running_apchi,
)

pytestmark = pytest.mark.tier2

SERVER_CONFIGURATION = "/api/v1/admin/server-configuration"
ADMIN_APPLIES = "/api/v1/admin/applies"


async def _settled(client: AsyncClient, apply_id: str, timeout: float = 900.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in ("succeeded", "failed", "incident"):
            return dict(record)
        await asyncio.sleep(0.5)
    raise AssertionError(f"apply {apply_id} never finished")


async def _admin_apply(client: AsyncClient) -> dict[str, Any]:
    started = await client.post(ADMIN_APPLIES, json={})
    assert started.status_code == 202, started.text
    return await _settled(client, started.json()["id"])


async def _set(client: AsyncClient, coordinator: dict[str, dict[str, str]]) -> None:
    response = await client.put(SERVER_CONFIGURATION, json={"coordinator": coordinator})
    assert response.status_code == 200, response.text


async def _set_role(client: AsyncClient, role: str, files: dict[str, dict[str, str]]) -> None:
    response = await client.put(SERVER_CONFIGURATION, json={role: files})
    assert response.status_code == 200, response.text


def _coordinator_pod() -> str:
    """The running coordinator's uid: a pod that was not replaced keeps it."""
    pods = json.loads(
        kubectl("get", "pods", "-l", "app.kubernetes.io/component=coordinator", "-o", "json")
    )["items"]
    running = _serving(pods)
    assert len(running) == 1, [pod["metadata"]["name"] for pod in pods]
    return str(running[0]["metadata"]["uid"])


async def _session_default(forward: PortForward, settings: Settings, name: str) -> str:
    trino = Trino(url=f"http://127.0.0.1:{forward.port}", user=settings.trino_user)
    rows = await trino.query(f"SHOW SESSION LIKE '{name}'")
    assert rows, f"no session property {name}"
    return str(rows[0][1])


async def test_an_admins_property_reaches_trino(
    e2e_client: AsyncClient, forward: PortForward, settings: Settings
) -> None:
    """The proof is Trino's own: a config.properties default shows as the session property's
    value. A file that landed somewhere Trino never looks would leave it at 100.00d."""
    await _set(e2e_client, {"config.properties": {"query.max-execution-time": "3h"}})

    record = await _admin_apply(e2e_client)

    assert record["stage"] == "succeeded", record
    assert record["rolled_out"] is True
    forward.restart()
    assert await _session_default(forward, settings, "query_max_execution_time") == "3.00h"


async def test_sizing_reaches_trino_from_the_pod_and_never_from_a_file(
    e2e_client: AsyncClient,
) -> None:
    """The chart sets the heap in JDK_JAVA_OPTIONS and query memory in the variables Apchi's
    config.properties refers to. Read back from what the running JVM and Trino report."""
    log = coordinator_log(lines=20000)

    assert "Picked up JDK_JAVA_OPTIONS: -Xmx2G" in log
    assert _runtime_value(log, "query.max-memory-per-node") == "600MB"
    jvm = kubectl("exec", COORDINATOR, "-c", "trino", "--", "cat", "/etc/trino/jvm.config")
    assert "Xmx" not in jvm, jvm


@pytest.mark.parametrize(
    ("coordinator", "trino_says"),
    [
        ({"config.properties": {"query.max-memroy": "1GB"}}, "was not used"),
        ({"config.properties": {"query.max-execution-time": "2hours"}}, "Invalid value"),
        ({"log.properties": {"io.trino": "DEBUGG"}}, "DEBUGG"),
    ],
)
async def test_what_trino_refuses_is_refused_on_the_probe_and_the_cluster_is_untouched(
    e2e_client: AsyncClient, coordinator: dict[str, dict[str, str]], trino_says: str
) -> None:
    before = _coordinator_pod()
    secret = _live_secret(SERVER_CONFIG_SECRET)
    await _set(e2e_client, coordinator)

    record = await _admin_apply(e2e_client)

    assert record["stage"] == "failed", record
    assert trino_says in (record["failure_reason"] or ""), record["failure_reason"]
    assert record["rolled_out"] is False
    assert _coordinator_pod() == before, "a probe refusal restarted the Cluster"
    assert _live_secret(SERVER_CONFIG_SECRET) == secret


async def test_a_property_only_the_cluster_cannot_run_with_is_rolled_back(
    settings: Settings,
    forwarded_kubernetes: ForwardedKubernetes,
    forward: PortForward,
    cluster_state: None,
) -> None:
    """`query.max-run-time=1ms` boots -- the probe only boots -- and then fails every query,
    Verification's included (measured on Trino 483). So the Cluster is where it is found out,
    and Auto Rollback is what puts the properties and the file back."""
    secret = _live_secret(SERVER_CONFIG_SECRET)
    impatient = settings.model_copy(update={"rollout_timeout_seconds": 300.0})
    async with running_apchi(impatient, forwarded_kubernetes, forward) as client:
        await _set(client, {"config.properties": {"query.max-run-time": "1ms"}})
        record = await _admin_apply(client)
        stored = (await client.get(SERVER_CONFIGURATION)).json()["coordinator"]

    assert record["stage"] == "failed", record
    assert record["rollback"] == "succeeded", record
    assert stored["config.properties"] == {}, "the failed properties were left in place"
    assert _live_secret(SERVER_CONFIG_SECRET) == secret
    forward.restart()
    trino = Trino(url=f"http://127.0.0.1:{forward.port}", user=settings.trino_user)
    assert await trino.query("SELECT 1") == [[1]]


async def test_the_secret_is_written_on_first_start_and_never_rewritten(
    settings: Settings,
    forwarded_kubernetes: ForwardedKubernetes,
    forward: PortForward,
    cluster_state: None,
) -> None:
    """Deleted, as on a new install: Apchi writes it. Then changed by hand: Apchi leaves it,
    because what is there is what the running coordinator started with."""
    kubectl("delete", "secret", SERVER_CONFIG_SECRET)
    async with running_apchi(settings, forwarded_kubernetes, forward):
        for _ in range(100):
            if (written := _live_secret(SERVER_CONFIG_SECRET)) is not None:
                break
            await asyncio.sleep(0.1)
    assert written == coordinator_secret(AdminValues().server_configuration, settings)

    held = {**written, "log.properties": "io.trino.held=INFO\n"}
    await forwarded_kubernetes.write_secret(SERVER_CONFIG_SECRET, held)
    async with running_apchi(settings, forwarded_kubernetes, forward):
        await asyncio.sleep(3)
    assert _live_secret(SERVER_CONFIG_SECRET) == held


async def test_a_mount_of_anyone_elses_under_etc_trino_refuses_the_apply(
    e2e_client: AsyncClient,
) -> None:
    """The ninth precondition against a real pod template: a chart that mounts a file of its
    own under /etc/trino again is refused before Apchi writes anything."""
    kubectl("create", "configmap", "apchi-test-foreign", "--from-literal=node.properties=x=y\n")
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "trino",
                            "volumeMounts": [
                                {
                                    "name": "foreign",
                                    "mountPath": "/etc/trino/exchange-manager.properties",
                                    "subPath": "node.properties",
                                }
                            ],
                        }
                    ],
                    "volumes": [{"name": "foreign", "configMap": {"name": "apchi-test-foreign"}}],
                }
            }
        }
    }
    kubectl("patch", "deployment", "trino-coordinator", "--type=strategic", "-p", json.dumps(patch))
    try:
        record = await _admin_apply(e2e_client)

        assert record["stage"] == "failed", record
        assert "/etc/trino/exchange-manager.properties" in record["failure_reason"]
        assert "ConfigMap 'apchi-test-foreign'" in record["failure_reason"]
    finally:
        undo = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "trino",
                                "volumeMounts": [
                                    {
                                        "mountPath": "/etc/trino/exchange-manager.properties",
                                        "$patch": "delete",
                                    }
                                ],
                            }
                        ],
                        "volumes": [{"name": "foreign", "$patch": "delete"}],
                    }
                }
            }
        }
        kubectl(
            "patch", "deployment", "trino-coordinator", "--type=strategic", "-p", json.dumps(undo)
        )
        kubectl("rollout", "status", COORDINATOR, "--timeout=600s")
        kubectl("delete", "configmap", "apchi-test-foreign", "--ignore-not-found")


# --- the workers (stage 2) ---------------------------------------------------------------------


def _runtime_value(log: str, prop: str) -> str:
    """The value Trino started with, from the table it logs at startup: property, default,
    runtime value, then a description that may be empty or several words -- so the runtime
    value is the third column, not the last."""
    import re

    found = re.findall(rf"\t{re.escape(prop)}\s+(\S+)\s+(\S+)", log)
    assert found, f"{prop} is not in the startup log"
    return str(found[-1][1])


def _serving(pods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Running and not on its way out. A draining worker still reports phase Running while its
    preStop hook waits for it to exit -- measured, 10s with a 5s grace -- so the phase alone
    counts it twice."""
    return [
        pod
        for pod in pods
        if pod["status"].get("phase") == "Running" and "deletionTimestamp" not in pod["metadata"]
    ]


def _pod_uid(component: str) -> str:
    pods = json.loads(
        kubectl("get", "pods", "-l", f"app.kubernetes.io/component={component}", "-o", "json")
    )["items"]
    running = _serving(pods)
    assert len(running) == 1, [pod["metadata"]["name"] for pod in pods]
    return str(running[0]["metadata"]["uid"])


def _worker_pod() -> str:
    pods = json.loads(
        kubectl("get", "pods", "-l", "app.kubernetes.io/component=worker", "-o", "json")
    )["items"]
    running = _serving(pods)
    assert len(running) == 1, [pod["metadata"]["name"] for pod in pods]
    return str(running[0]["metadata"]["name"])


async def test_a_workers_property_reaches_the_workers_after_a_drain_and_the_coordinator_stays(
    e2e_client: AsyncClient,
) -> None:
    """The whole of stage 2 in one run: the property reaches the worker, the old worker
    drains before it goes -- read from its own log, captured while it ran -- and the
    coordinator, which cannot drain, is never touched."""
    import subprocess

    coordinator = _pod_uid("coordinator")
    old_worker = _worker_pod()
    captured = subprocess.Popen(
        ["kubectl", "logs", "-f", f"pod/{old_worker}", "-c", "trino", "--tail=0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        await _set_role(e2e_client, "worker", {"config.properties": {"task.concurrency": "8"}})
        record = await _admin_apply(e2e_client)
        # The rollout completes when the new worker is ready, which is when the old one
        # *starts* draining. Its log is only whole once it has gone.
        kubectl("wait", "--for=delete", f"pod/{old_worker}", "--timeout=120s")
    finally:
        captured.terminate()
        drained = captured.communicate(timeout=30)[0]

    assert record["stage"] == "succeeded", record
    assert _pod_uid("coordinator") == coordinator, "a worker-only change restarted the coordinator"
    assert _worker_pod() != old_worker, "the worker was not replaced"
    assert "Shutdown requested" in drained, drained[-2000:]
    assert "All active tasks are finished" in drained, drained[-2000:]
    log = kubectl("logs", "deploy/trino-worker", "-c", "trino", "--tail=20000")
    assert _runtime_value(log, "task.concurrency") == "8"


async def test_only_apchis_identity_may_drain_a_worker(e2e_client: AsyncClient) -> None:
    """The workers' own rules grant one identity one thing. Asked by anyone else, the worker
    refuses and keeps serving."""
    worker = _worker_pod()

    status = kubectl(
        "exec",
        f"pod/{worker}",
        "-c",
        "trino",
        "--",
        "curl",
        "-s",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "-X",
        "PUT",
        "-d",
        '"SHUTTING_DOWN"',
        "-H",
        "Content-Type: application/json",
        "-H",
        "X-Trino-User: intruder",
        "http://localhost:8080/v1/info/state",
    )

    assert status == "403", status
    # Still serving: the readiness probe's own check.
    kubectl("exec", f"pod/{worker}", "-c", "trino", "--", "/usr/lib/trino/bin/health-check")
    assert _worker_pod() == worker


async def test_a_misspelled_worker_property_is_refused_on_the_probe(
    e2e_client: AsyncClient,
) -> None:
    coordinator, worker = _pod_uid("coordinator"), _worker_pod()
    await _set_role(e2e_client, "worker", {"config.properties": {"task.concurency": "8"}})

    record = await _admin_apply(e2e_client)

    assert record["stage"] == "failed", record
    assert "was not used" in (record["failure_reason"] or ""), record["failure_reason"]
    assert (_pod_uid("coordinator"), _worker_pod()) == (coordinator, worker)


async def test_a_workers_value_the_coordinator_does_not_share_is_proved_on_its_own_probe(
    e2e_client: AsyncClient,
) -> None:
    """The coordinator's value is valid and the workers' is not. One probe holding the
    coordinator's would have passed both."""
    worker = _worker_pod()
    await e2e_client.put(
        SERVER_CONFIGURATION,
        json={
            "coordinator": {"config.properties": {"task.max-worker-threads": "8"}},
            "worker": {"config.properties": {"task.max-worker-threads": "many"}},
        },
    )

    record = await _admin_apply(e2e_client)

    assert record["stage"] == "failed", record
    assert "The workers' properties" in (record["failure_reason"] or ""), record
    assert _worker_pod() == worker


# --- the workers' disruption budget ------------------------------------------------------------


def _evict(pod: str) -> subprocess.CompletedProcess[str]:
    """Ask the Eviction API for the pod, as a node drain does -- the call a budget governs.
    Deleting the pod directly would bypass the budget and prove nothing."""
    eviction = json.dumps(
        {
            "apiVersion": "policy/v1",
            "kind": "Eviction",
            "metadata": {"name": pod, "namespace": "default"},
        }
    )
    return subprocess.run(
        [
            "kubectl",
            "create",
            "--raw",
            f"/api/v1/namespaces/default/pods/{pod}/eviction",
            "-f",
            "-",
        ],
        input=eviction,
        capture_output=True,
        text=True,
    )


def _serving_workers() -> list[str]:
    pods = json.loads(
        kubectl("get", "pods", "-l", "app.kubernetes.io/component=worker", "-o", "json")
    )["items"]
    return sorted(str(pod["metadata"]["name"]) for pod in _serving(pods))


async def test_a_node_drain_takes_one_worker_at_a_time_and_each_drains(
    e2e_client: AsyncClient,
) -> None:
    """With two workers and the default budget, one may be evicted -- and it drains, read from
    its own log -- and the second is refused until the first is replaced. One worker cannot
    show the refusal: Kubernetes lets an eviction through when nothing healthy is left to
    protect."""
    budget = json.loads(kubectl("get", "pdb", "trino-worker", "-o", "json"))
    assert budget["status"]["expectedPods"] == 1, budget["status"]

    kubectl("scale", WORKER, "--replicas=2")
    try:
        kubectl("rollout", "status", WORKER, "--timeout=600s")
        for _ in range(60):
            budget = json.loads(kubectl("get", "pdb", "trino-worker", "-o", "json"))
            if budget["status"].get("currentHealthy") == 2:
                break
            await asyncio.sleep(2)
        assert budget["status"]["disruptionsAllowed"] == 1, budget["status"]
        first, second = _serving_workers()
        captured = subprocess.Popen(
            ["kubectl", "logs", "-f", f"pod/{first}", "-c", "trino", "--tail=0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        allowed = _evict(first)
        refused = _evict(second)
        kubectl("wait", "--for=delete", f"pod/{first}", "--timeout=120s")
        captured.terminate()
        drained = captured.communicate(timeout=30)[0]

        assert allowed.returncode == 0, allowed.stderr
        assert refused.returncode != 0, "the budget let a second worker go at once"
        assert "disruption budget" in refused.stderr, refused.stderr
        assert "Shutdown requested" in drained, drained[-2000:]
        assert second in _serving_workers(), "the refused worker was taken anyway"
    finally:
        kubectl("scale", WORKER, "--replicas=1")
        kubectl("rollout", "status", WORKER, "--timeout=600s")

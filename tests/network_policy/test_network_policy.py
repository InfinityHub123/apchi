"""charts/apchi's NetworkPolicy, enforced: who reaches Trino, and Trino still working under it.

Its own directory and its own CI job, because it needs a network plugin that enforces policy.
kind's default, kindnet, does not -- there a policy is accepted and changes nothing -- so the
job runs kind with Calico, and this refuses to run anywhere it would pass vacuously.

The policies are the chart's own render, applied to the Trino deploy/trino-dev runs, and the
clients are pods inside and outside its namespace. Each is asked to reach the coordinator
through its Service and a worker by its pod IP, the two ways Trino is reached.
"""

import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "trinodb/trino:483"
#: The namespace standing in for a Trino Gateway: allowed to the coordinator by an extra peer,
#: as an operator would list theirs.
GATEWAY_NAMESPACE = "apchi-np-gateway"
#: A namespace nobody listed, standing in for the rest of the cluster.
OUTSIDE_NAMESPACE = "apchi-np-outside"


def kubectl(*args: str, stdin: str | None = None, check: bool = True) -> str:
    finished = subprocess.run(
        ["kubectl", *args], input=stdin, capture_output=True, text=True, timeout=600
    )
    if check and finished.returncode != 0:
        raise AssertionError(f"kubectl {' '.join(args)}: {finished.stderr}")
    return finished.stdout


def _enforcing() -> bool:
    found = subprocess.run(
        ["kubectl", "get", "pods", "-n", "kube-system", "-l", "k8s-app=calico-node", "-o", "name"],
        capture_output=True,
        text=True,
    )
    return found.returncode == 0 and bool(found.stdout.strip())


@pytest.fixture(scope="module", autouse=True)
def policies() -> Iterator[None]:
    """The chart's policies, rendered for the dev fixture with a gateway namespace allowed."""
    if not _enforcing():
        # Skipped locally, refused in CI: a job that set up Calico and then skipped would be
        # green having tested nothing.
        if os.environ.get("REQUIRE_POLICY_ENFORCEMENT"):
            pytest.fail(
                "this job must run on a cluster that enforces NetworkPolicy; Calico is missing"
            )
        pytest.skip("no NetworkPolicy-enforcing network plugin (Calico) in this cluster")
    values = {
        "networkPolicy": {
            "enabled": True,
            "coordinator": {
                "from": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": GATEWAY_NAMESPACE}
                        }
                    }
                ]
            },
        }
    }
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "trino",
            str(ROOT / "charts" / "apchi"),
            "--values",
            str(ROOT / "deploy" / "trino-dev.values.yaml"),
            "--set",
            "mongodb.deploy=true",
            "--values",
            "-",
            "--show-only",
            "templates/trino-network-policy.yaml",
        ],
        input=json.dumps(values),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for namespace in (GATEWAY_NAMESPACE, OUTSIDE_NAMESPACE):
        kubectl("create", "namespace", namespace, check=False)
    kubectl("apply", "-f", "-", stdin=rendered)
    # Calico programs a policy within a second or two; a client started straight after could
    # race it and be let through by a policy not yet in force.
    time.sleep(5)
    try:
        yield
    finally:
        kubectl("delete", "-f", "-", "--ignore-not-found", stdin=rendered)
        for namespace in (GATEWAY_NAMESPACE, OUTSIDE_NAMESPACE):
            kubectl("delete", "namespace", namespace, "--ignore-not-found", "--wait=false")


def _client(labels: dict[str, str], namespace: str = "default") -> str:
    """A pod labelled like one of the chart's, with curl in it. The Trino image has curl and is
    already on the node."""
    name = f"np-{uuid.uuid4().hex[:8]}"
    overrides = {"metadata": {"labels": labels}} if labels else {}
    kubectl(
        "run",
        name,
        "-n",
        namespace,
        f"--image={IMAGE}",
        "--restart=Never",
        "--image-pull-policy=IfNotPresent",
        f"--overrides={json.dumps(overrides)}",
        "--command",
        "--",
        "sleep",
        "600",
    )
    kubectl("wait", "-n", namespace, "--for=condition=ready", f"pod/{name}", "--timeout=180s")
    return name


def _reaches(client: str, url: str, namespace: str = "default") -> bool:
    """Whether the client gets any HTTP answer at all within a few seconds. A refusal by policy
    is a dropped connection, so it shows as no answer -- curl's 000."""
    code = kubectl(
        "exec",
        "-n",
        namespace,
        client,
        "--",
        "curl",
        "-s",
        "-m",
        "5",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        url,
        check=False,
    ).strip()
    return code not in ("", "000")


def _worker_url() -> str:
    pods = json.loads(
        kubectl("get", "pods", "-l", "app.kubernetes.io/component=worker", "-o", "json")
    )["items"]
    running = [p for p in pods if p["status"].get("phase") == "Running"]
    return f"http://{running[0]['status']['podIP']}:8080/v1/info"


COORDINATOR = "http://trino.default.svc:8080/v1/info"


def test_a_pod_in_another_namespace_reaches_neither_role() -> None:
    """The point of the policy: reaching the HTTP port is being able to claim any user."""
    stranger = _client({}, namespace=OUTSIDE_NAMESPACE)
    try:
        assert not _reaches(stranger, COORDINATOR, namespace=OUTSIDE_NAMESPACE)
        assert not _reaches(stranger, _worker_url(), namespace=OUTSIDE_NAMESPACE)
    finally:
        kubectl("delete", "pod", "-n", OUTSIDE_NAMESPACE, stranger, "--wait=false")


def test_any_pod_in_trinos_namespace_reaches_both_roles() -> None:
    """Apchi, and a scraper such as Alloy reading /metrics, with no label the chart knows."""
    neighbour = _client({})
    try:
        assert _reaches(neighbour, COORDINATOR)
        assert _reaches(neighbour, _worker_url())
    finally:
        kubectl("delete", "pod", neighbour, "--wait=false")


def test_a_listed_namespace_reaches_the_coordinator_and_not_the_workers() -> None:
    gateway = _client({}, namespace=GATEWAY_NAMESPACE)
    try:
        assert _reaches(gateway, COORDINATOR, namespace=GATEWAY_NAMESPACE)
        assert not _reaches(gateway, _worker_url(), namespace=GATEWAY_NAMESPACE)
    finally:
        kubectl("delete", "pod", "-n", GATEWAY_NAMESPACE, gateway, "--wait=false")


def test_trino_still_runs_queries_across_its_own_pods() -> None:
    """Under the policy, a query still reaches the worker and comes back: the coordinator
    schedules no tasks on itself, so a count over a table is the worker's work end to end."""
    out = kubectl(
        "exec",
        "deploy/trino-coordinator",
        "-c",
        "trino",
        "--",
        "trino",
        "--execute",
        "SELECT count(*) FROM system.runtime.nodes WHERE state = 'active'",
    )
    assert out.strip().strip('"') == "2", out
    out = kubectl(
        "exec",
        "deploy/trino-coordinator",
        "-c",
        "trino",
        "--",
        "trino",
        "--execute",
        "SELECT count(*) FROM tpch.tiny.nation",
    )
    assert out.strip().strip('"') == "25", out

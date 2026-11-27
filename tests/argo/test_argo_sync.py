"""charts/apchi under Argo CD: an ordinary sync must not undo what Apchi applied.

Found on kind, 2026-11-25. The chart created the Secrets Apchi writes into as Helm pre-install
hooks -- created once by Helm and never again. Argo CD does not run Helm: it renders the chart
and turns those hooks into its own sync hooks, which it runs on **every** sync, deleting and
recreating each Secret with the chart's starting contents. One sync for an unrelated pod
annotation emptied the catalog seed, reset the access-control rules to fail-open, and emptied
the Event Listener and Resource Groups Secrets while Apchi's mounts still read from them -- so
the restarted coordinator crash-looped.

It installs the chart's default shape, certificate authentication (#295), so it is also where a
real coordinator proves that shape: HTTPS refuses a client that sends only a user name and admits
one with a certificate, while Apchi, which has none, works over the HTTP port; the workers join
on the shared secret Apchi created; and the sync keeps that secret, which the chart does not
render.

So this drives the real chart through a real Argo CD, with automated sync, prune and self-heal:
Apchi applies a catalog, a permission, an Event Listener, Resource Groups and a Raw File through
its own API, then the chart changes and Argo syncs, and everything Apchi wrote has to survive,
with the coordinator coming back healthy on it.

Its own directory and CI job: it installs Argo CD and serves the chart from a git daemon inside
the cluster, which takes minutes and has nothing to do with the tier 2 shards.

Needs a kind cluster named by $KIND_CLUSTER (default `apchi`) with no Trino in `default`, and
Apchi's image loaded into it as $APCHI_IMAGE (default `apchi:argotest`).
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import trino

from tests.certificates import tls_chain

ROOT = Path(__file__).resolve().parents[2]
ARGO_VERSION = "v2.13.3"
IMAGE = os.environ.get("APCHI_IMAGE", "apchi:argotest")
REPO = "git://gitd.argocd.svc.cluster.local/apchi.git"

CATALOG = {"name": "argotest", "connector": "memory", "properties": {}}
GRANT = {"identity": "analyst", "catalog": "argotest", "privileges": ["SELECT"]}
#: The Admin's, in the coordinator's Server Configuration (#269): no mount of its own.
LISTENER = {
    "event-listener.name": "http",
    "http-event-listener.connect-ingest-uri": "http://localhost:9/events",
}
RAW_FILE = "/etc/trino/apchi-raw-test.properties"

#: Small enough for a laptop and a CI runner: a coordinator rollout runs two coordinators
#: beside the worker, and at the chart's 2G heaps that alone is most of a 7 GB runner.
SMALL: dict[str, Any] = {
    "jvm": {"maxHeapSize": "768M"},
    "memory": {"maxMemoryPerNode": "256MB", "heapHeadroomPerNode": "256MB"},
}


def kubectl(*args: str, stdin: str | None = None, check: bool = True, timeout: int = 900) -> str:
    finished = subprocess.run(
        ["kubectl", *args], input=stdin, capture_output=True, text=True, timeout=timeout
    )
    if check and finished.returncode != 0:
        raise AssertionError(f"kubectl {' '.join(args)}: {finished.stderr}")
    return finished.stdout


#: The certificates the installer provides: Apchi issues none. One CA signs the coordinator's
#: server certificate and the certificate a client outside the namespace signs in with.
CHAIN = tls_chain(server_name="trino", client_name="apchi")
SHARED_SECRET = "trino-internal-shared-secret"


def _installers_secrets() -> None:
    def secret(name: str, data: dict[str, bytes]) -> dict[str, Any]:
        encoded = {key: base64.b64encode(value).decode() for key, value in data.items()}
        return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name}, "data": encoded}

    items = [
        secret(
            "trino-server-tls",
            {"tls.crt": CHAIN.server, "tls.key": CHAIN.server_key, "ca.crt": CHAIN.ca},
        ),
    ]
    kubectl(
        "apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": items})
    )


def _secret(name: str) -> dict[str, str]:
    raw = kubectl("get", "secret", name, "-o", "json")
    data = json.loads(raw).get("data") or {}
    return {key: base64.b64decode(value).decode() for key, value in data.items()}


def _values(annotation: str) -> dict[str, Any]:
    return {
        "image": {
            "repository": IMAGE.split(":")[0],
            "tag": IMAGE.split(":")[1],
            "pullPolicy": "Never",
        },
        "mongodb": {"deploy": True},
        "trino": {
            # The server certificate, and nothing for Apchi: it reaches Trino over HTTP.
            "https": {"existingSecret": "trino-server-tls"},
            "securityRefreshPeriod": "1s",
            "coordinator": {"podAnnotations": {"apchi-argo-test": annotation}, **SMALL},
            "worker": {
                **SMALL,
                "replicas": 1,
                "gracefulShutdown": {"gracePeriodSeconds": 5},
                "terminationGracePeriodSeconds": 30,
            },
        },
    }


def _application(annotation: str) -> dict[str, Any]:
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "apchi", "namespace": "argocd"},
        "spec": {
            "project": "default",
            "source": {
                "repoURL": REPO,
                "targetRevision": "main",
                "path": "charts/apchi",
                "helm": {"valuesObject": _values(annotation)},
            },
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "default"},
            "syncPolicy": {"automated": {"prune": True, "selfHeal": True}},
        },
    }


def _app() -> dict[str, Any]:
    return dict(json.loads(kubectl("-n", "argocd", "get", "application", "apchi", "-o", "json")))


def _await(condition: str, check: Any, timeout: float = 900) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(5)
    raise AssertionError(f"timed out waiting for {condition}")


def _synced_and_healthy() -> bool:
    status = _app().get("status", {})
    return (
        status.get("sync", {}).get("status") == "Synced"
        and status.get("health", {}).get("status") == "Healthy"
    )


@pytest.fixture(scope="module")
def argo() -> Iterator[None]:
    """Argo CD, a git daemon in the cluster holding the chart from this checkout, and the chart
    deployed through them."""
    kubectl("create", "namespace", "argocd", check=False)
    kubectl(
        "apply",
        "-n",
        "argocd",
        "--server-side",
        "-f",
        f"https://raw.githubusercontent.com/argoproj/argo-cd/{ARGO_VERSION}/manifests/install.yaml",
    )
    kubectl("-n", "argocd", "rollout", "status", "deploy/argocd-repo-server", "--timeout=600s")
    kubectl(
        "-n",
        "argocd",
        "rollout",
        "status",
        "statefulset/argocd-application-controller",
        "--timeout=600s",
    )
    _serve_chart()
    _installers_secrets()
    kubectl("apply", "-f", "-", stdin=json.dumps(_application("first")))
    _await("the chart to sync and come up healthy", _synced_and_healthy)
    yield


def _serve_chart() -> None:
    """The chart, committed to a bare repository a git daemon in the cluster serves."""
    kubectl(
        "apply",
        "-f",
        "-",
        stdin=json.dumps(
            {
                "apiVersion": "v1",
                "kind": "List",
                "items": [
                    {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "metadata": {
                            "name": "gitd",
                            "namespace": "argocd",
                            "labels": {"app": "gitd"},
                        },
                        "spec": {
                            "containers": [
                                {
                                    "name": "git",
                                    "image": "alpine/git:2.47.2",
                                    "command": [
                                        "sh",
                                        "-c",
                                        "apk add -q git-daemon && sleep infinity",
                                    ],
                                }
                            ]
                        },
                    },
                    {
                        "apiVersion": "v1",
                        "kind": "Service",
                        "metadata": {"name": "gitd", "namespace": "argocd"},
                        "spec": {"selector": {"app": "gitd"}, "ports": [{"port": 9418}]},
                    },
                ],
            }
        ),
    )
    kubectl("-n", "argocd", "wait", "--for=condition=ready", "pod/gitd", "--timeout=300s")
    with tempfile.TemporaryDirectory() as work:
        repo = Path(work) / "repo"
        shutil.copytree(ROOT / "charts" / "apchi", repo / "charts" / "apchi")
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "chart"], check=True)
        bundle = Path(work) / "apchi.bundle"
        subprocess.run(
            [*git, "bundle", "create", str(bundle), "main"], check=True, capture_output=True
        )
        kubectl("-n", "argocd", "cp", str(bundle), "gitd:/tmp/apchi.bundle")
    deadline = time.monotonic() + 120
    while "git-daemon" not in kubectl(
        "-n",
        "argocd",
        "exec",
        "gitd",
        "--",
        "sh",
        "-c",
        "ls /usr/libexec/git-core/ || true",
        check=False,
    ):
        if time.monotonic() > deadline:
            raise AssertionError("git-daemon never installed in the gitd pod")
        time.sleep(2)
    kubectl(
        "-n",
        "argocd",
        "exec",
        "gitd",
        "--",
        "sh",
        "-c",
        "git clone -q --bare /tmp/apchi.bundle /srv/git/apchi.git"
        " && git -C /srv/git/apchi.git symbolic-ref HEAD refs/heads/main"
        " && git daemon --base-path=/srv/git --export-all --reuseaddr --detach /srv/git",
    )


class _Apchi:
    """Apchi's API through a port-forward to its Service."""

    def __init__(self) -> None:
        self._forward = subprocess.Popen(
            ["kubectl", "port-forward", "svc/apchi", "18000:8000"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.http = httpx.Client(base_url="http://127.0.0.1:18000/api/v1", timeout=60)
        _await_answer(self.http)

    def apply(self, path: str = "/applies") -> dict[str, Any]:
        started = self.http.post(path, json={})
        assert started.status_code == 202, started.text
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            record = self.http.get(f"/applies/{started.json()['id']}").json()
            if record["stage"] in ("succeeded", "failed", "incident"):
                return dict(record)
            time.sleep(3)
        raise AssertionError("the Apply never settled")

    def close(self) -> None:
        self.http.close()
        self._forward.terminate()


class _Coordinator:
    """The coordinator's HTTPS port through a port-forward, signed in with a certificate, as a
    client outside the namespace would be."""

    def __init__(self) -> None:
        self._work = tempfile.TemporaryDirectory()
        work = Path(self._work.name)
        for name, content in (
            ("ca.pem", CHAIN.ca),
            ("client.pem", CHAIN.client),
            ("client-key.pem", CHAIN.client_key),
        ):
            (work / name).write_bytes(content)
        self.ca = str(work / "ca.pem")
        self._client = (str(work / "client.pem"), str(work / "client-key.pem"))
        self._forward = subprocess.Popen(
            ["kubectl", "port-forward", "svc/trino", "18443:8443"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.https = "https://127.0.0.1:18443"
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                httpx.get(f"{self.https}/v1/info", verify=self.ca, timeout=5)
                return
            except httpx.TransportError:
                time.sleep(1)
        raise AssertionError("the coordinator never answered through the port-forward")

    def query(self, sql: str) -> list[list[Any]]:
        connection = trino.dbapi.connect(
            host=self.https,
            user="apchi",
            auth=trino.auth.CertificateAuthentication(*self._client),
            verify=self.ca,
        )
        try:
            cursor = connection.cursor()
            cursor.execute(sql)
            return [list(row) for row in cursor.fetchall()]
        finally:
            connection.close()

    def __enter__(self) -> "_Coordinator":
        return self

    def __exit__(self, *_: object) -> None:
        self._forward.terminate()
        self._work.cleanup()


def _await_answer(http: httpx.Client) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if http.get("/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(1)
    raise AssertionError("Apchi never answered through the port-forward")


def _apchi_volumes() -> set[str]:
    names = kubectl(
        "get", "deploy", "trino-coordinator", "-o", "jsonpath={.spec.template.spec.volumes[*].name}"
    ).split()
    return {
        n
        for n in names
        if n in ("apchi-resource-groups", "apchi-admin-raw-files", "apchi-event-listener")
    }


def _template_annotations() -> dict[str, str]:
    return dict(
        json.loads(
            kubectl(
                "get",
                "deploy",
                "trino-coordinator",
                "-o",
                "jsonpath={.spec.template.metadata.annotations}",
            )
        )
    )


def _deployment_annotations() -> dict[str, str]:
    return dict(
        json.loads(
            kubectl("get", "deploy", "trino-coordinator", "-o", "jsonpath={.metadata.annotations}")
        )
    )


def test_an_argo_sync_keeps_everything_apchi_applied(argo: None) -> None:
    apchi = _Apchi()
    try:
        for path, body in (
            ("/catalogs", CATALOG),
            ("/permissions", GRANT),
            ("/resource-groups", {"path": "global", "hard_concurrency_limit": 10}),
        ):
            response = apchi.http.post(path, json=body)
            assert response.status_code in (200, 201), f"{path}: {response.text}"
        # A client signing in to HTTPS is its certificate's subject, CN=apchi; this makes it
        # the user its queries are checked as.
        mapping = apchi.http.put("/certificate-mapping", json={"pattern": "CN=(.*)", "user": "$1"})
        assert mapping.status_code == 200, mapping.text
        selectors = apchi.http.put(
            "/resource-groups/selectors", json={"selectors": [{"group": "global"}]}
        )
        assert selectors.status_code == 200, selectors.text
        raw = apchi.http.put(f"/admin/files{RAW_FILE}", json={"content": "apchi.raw-test=one\n"})
        assert raw.status_code == 200, raw.text
        record = apchi.apply()
        assert record["stage"] == "succeeded", record
        listener = apchi.http.put(
            "/admin/server-configuration",
            json={"coordinator": {"event-listener.properties": LISTENER}},
        )
        assert listener.status_code == 200, listener.text
        record = apchi.apply("/admin/applies")
        assert record["stage"] == "succeeded", record
    finally:
        apchi.close()

    applied = {
        name: _secret(name)
        for name in (
            "trino-catalog-seed",
            "trino-access-control",
            "trino-user-mapping",
            "trino-server-config-coordinator",
            "trino-resource-groups",
            "trino-client-certificates",
        )
    }
    assert "argotest.properties" in applied["trino-catalog-seed"]
    assert "analyst" in applied["trino-access-control"]["rules.json"]
    assert "event-listener.properties" in applied["trino-server-config-coordinator"], (
        "the listener was never written"
    )
    assert _apchi_volumes() == {"apchi-resource-groups", "apchi-admin-raw-files"}
    checksum = _template_annotations()["apchi/rolled-out-checksum"]
    shared = _secret(SHARED_SECRET)
    verified = _deployment_annotations()["apchi/verified-snapshot"]
    syncs_before = len(_app()["status"].get("history", []))

    # An ordinary chart change: the kind of commit a platform team makes every week. It changes
    # the coordinator's pod template, so Argo syncs and the coordinator restarts.
    kubectl(
        "-n",
        "argocd",
        "patch",
        "application",
        "apchi",
        "--type=merge",
        "-p",
        json.dumps({"spec": {"source": {"helm": {"valuesObject": _values("second")}}}}),
    )
    _await(
        "Argo to sync the change",
        lambda: len(_app()["status"].get("history", [])) > syncs_before,
    )
    # Before waiting on health: a sync that resets these leaves a coordinator that never comes
    # back, and the failure should name the Secret rather than time out on the symptom.
    for name, before in applied.items():
        assert _secret(name) == before, f"the sync changed {name}, which Apchi had written"
    _await("the chart to come back synced and healthy", _synced_and_healthy)

    assert _apchi_volumes() == {"apchi-resource-groups", "apchi-admin-raw-files"}
    annotations = _template_annotations()
    assert annotations.get("apchi-argo-test") == "second", "the chart change never reached the pod"
    # The chart renders neither, so the sync keeps both -- and the pods the chart change rolled
    # out still carry the checksum of the files Apchi delivered, which did not change (#290).
    assert annotations.get("apchi/rolled-out-checksum") == checksum, annotations
    assert _deployment_annotations().get("apchi/verified-snapshot") == verified
    pods = json.loads(
        kubectl("get", "pods", "-l", "app.kubernetes.io/component=coordinator", "-o", "json")
    )["items"]
    carried = {
        pod["metadata"].get("annotations", {}).get("apchi/rolled-out-checksum")
        for pod in pods
        if "deletionTimestamp" not in pod["metadata"]
    }
    assert carried == {checksum}, carried
    assert _secret(SHARED_SECRET) == shared, "the sync replaced the shared secret Apchi created"
    with _Coordinator() as coordinator:
        catalogs = coordinator.query("SHOW CATALOGS")
        assert ["argotest"] in catalogs, (
            f"the restarted coordinator lost the catalog Apchi applied: {catalogs}"
        )


def test_the_coordinator_authenticates_by_certificate(argo: None) -> None:
    """The chart's default, on a real coordinator: HTTPS demands a certificate, and the workers
    joined on the shared secret Apchi created."""
    shared = _secret(SHARED_SECRET)
    assert list(shared) == ["internal-communication.shared-secret"], shared
    assert len(shared["internal-communication.shared-secret"]) >= 64

    with _Coordinator() as coordinator:
        # A query needs a worker -- the coordinator schedules none of its own -- so an answer
        # here is the worker's internal requests accepted on the shared secret.
        nodes = coordinator.query("SELECT coordinator FROM system.runtime.nodes")
        assert sorted(nodes) == [[False], [True]], nodes

        # Only a user name, over HTTPS: refused. HTTP would take it, which is why the
        # NetworkPolicy keeps that port to Trino and Apchi -- a port-forward goes around a
        # policy, and kind's network plugin enforces none, so that half is the
        # NetworkPolicy job's and tier 1's.
        refused = httpx.post(
            f"{coordinator.https}/v1/statement",
            content="SELECT 1",
            headers={"X-Trino-User": "analyst"},
            verify=coordinator.ca,
            timeout=30,
        )
        assert refused.status_code == 401, (refused.status_code, refused.text)

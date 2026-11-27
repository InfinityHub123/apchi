"""Every container the chart and Apchi run meets Kubernetes' "restricted" Pod Security level, and
Trino has startup and liveness probes, as charts/apchi renders them.

Read back from Helm's render, across the shapes that add containers -- HTTPS adds an
initContainer, a truststore adds mounts -- because a container added later without the
securityContext is exactly what a hand-picked check would miss. That a restricted namespace
actually admits them is tier 2's (tests/tier2/test_validation_pod.py).
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.pipeline.validation import validation_manifest

CHART = Path(__file__).resolve().parents[2] / "charts" / "apchi"
RESTRICTED = {
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
    "runAsNonRoot": True,
    "seccompProfile": {"type": "RuntimeDefault"},
}
SHAPES = {
    "default": (),
    "https": ("--set", "trino.https.enabled=true", "--set", "trino.https.existingSecret=tls"),
    "trust": (
        "--set",
        "caBundle.configMap=ca",
        "--set",
        "javaTruststore.existingSecret=ts",
        "--set",
        "javaTruststore.password.existingSecret=tsp",
        "--set",
        "javaTruststore.password.key=k",
    ),
}


def _render(*values: str) -> list[dict[str, Any]]:
    finished = subprocess.run(
        [
            "helm",
            "template",
            "rel",
            str(CHART),
            "--values",
            str(CHART / "ci" / "certificate.yaml"),
            *values,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [d for d in yaml.safe_load_all(finished.stdout) if d]


def _containers(documents: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Every container and initContainer of every workload, but the bundled MongoDB's: it is for
    trying Apchi out, and its image runs as root."""
    found = []
    for document in documents:
        if document["kind"] not in ("Deployment", "StatefulSet", "Pod", "Job"):
            continue
        spec = (
            document["spec"]["template"]["spec"]
            if "template" in document["spec"]
            else document["spec"]
        )
        name = document["metadata"]["name"]
        if name.endswith("mongodb"):
            continue
        for container in spec.get("initContainers", []) + spec.get("containers", []):
            found.append((f"{name}/{container['name']}", container))
    return found


@pytest.mark.parametrize("shape", list(SHAPES))
def test_every_container_the_chart_runs_is_restricted(shape: str) -> None:
    containers = _containers(_render(*SHAPES[shape]))

    assert len(containers) >= 4, [name for name, _ in containers]
    for name, container in containers:
        context = container.get("securityContext") or {}
        expected = dict(RESTRICTED)
        if "apchi" not in name.split("/")[0]:
            # Trino's image names its user rather than numbering it, and with runAsNonRoot the
            # kubelet refuses a name it cannot verify -- measured on kind. 1000 is `trino`.
            expected["runAsUser"] = 1000
        assert context == expected, name


def test_under_restricted_pod_security_the_platform_assigns_trinos_uid() -> None:
    """OpenShift's restricted-v2 assigns the uid and refuses one fixed in the pod spec."""
    containers = _containers(
        _render("--set", "restrictedPodSecurity=true", "--set", "trino.fsGroup=null")
    )

    for name, container in containers:
        assert container["securityContext"] == RESTRICTED, name


def test_the_https_shape_hardens_its_extra_initcontainer_too() -> None:
    names = [name for name, _ in _containers(_render(*SHAPES["https"]))]

    assert any("join-https-keystore" in name for name in names), names


def test_the_validation_pod_apchi_builds_is_restricted() -> None:
    """Apchi creates this one itself, so no chart value could fix it: a namespace enforcing the
    restricted level would refuse it, and every Validation fail before Trino had run."""
    manifest = validation_manifest("p", "trinodb/trino:483", 360, {"/etc/trino/x": "y"})

    for container in manifest["spec"]["containers"]:
        assert container["securityContext"] == {**RESTRICTED, "runAsUser": 1000}
    restricted = validation_manifest("p", "trinodb/trino:483", 360, {}, restricted=True)
    assert restricted["spec"]["containers"][0]["securityContext"] == RESTRICTED


@pytest.mark.parametrize("role", ["coordinator", "worker"])
def test_both_roles_have_a_startup_and_a_liveness_probe_running_trinos_health_check(
    role: str,
) -> None:
    documents = _render()
    trino = next(
        c
        for d in documents
        if d["kind"] == "Deployment"
        and d["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/component") == role
        and d["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/name") == "trino"
        for c in d["spec"]["template"]["spec"]["containers"]
        if c["name"] == "trino"
    )

    command = ["/usr/lib/trino/bin/health-check"]
    assert trino["startupProbe"]["exec"]["command"] == command
    assert trino["livenessProbe"]["exec"]["command"] == command
    assert trino["readinessProbe"]["exec"]["command"] == command
    # Loose: minutes of no answer, never one long GC pause.
    liveness = trino["livenessProbe"]
    assert liveness["periodSeconds"] * liveness["failureThreshold"] >= 120
    startup = trino["startupProbe"]
    assert startup["periodSeconds"] * startup["failureThreshold"] >= 300


def test_liveness_can_be_turned_off_per_role() -> None:
    documents = _render("--set", "trino.coordinator.livenessProbe.enabled=false")

    probes = {
        d["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"]: "livenessProbe"
        in d["spec"]["template"]["spec"]["containers"][0]
        for d in documents
        if d["kind"] == "Deployment"
        and d["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/name") == "trino"
    }

    assert probes == {"coordinator": False, "worker": True}

"""The workers' disruption budget and Trino's NetworkPolicy, as charts/apchi renders them.

Rendered with Helm and read back, and every selector checked against the labels on the pods the
same render produces -- not against a label written here. A selector that matches no pod is a
budget that protects nothing and a policy that lets nothing through, and both render cleanly.

Enforcement itself is not provable here: that needs a network plugin that enforces policy, and
tests/network_policy runs against one.
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "charts" / "apchi"
BASE = ("--set", "mongodb.deploy=true")


def _render(*values: str) -> list[dict[str, Any]]:
    finished = subprocess.run(
        ["helm", "template", "rel", str(CHART), *BASE, *values],
        capture_output=True,
        text=True,
        check=True,
    )
    return [d for d in yaml.safe_load_all(finished.stdout) if d]


def _refused(*values: str) -> str:
    finished = subprocess.run(
        ["helm", "template", "rel", str(CHART), *BASE, *values],
        capture_output=True,
        text=True,
    )
    assert finished.returncode != 0, "the chart rendered what it should have refused"
    return finished.stderr


def _of(documents: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [d for d in documents if d["kind"] == kind]


def _pod_labels(documents: list[dict[str, Any]], component: str, name: str) -> dict[str, str]:
    for deployment in _of(documents, "Deployment"):
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        if (
            labels.get("app.kubernetes.io/component") == component
            and labels.get("app.kubernetes.io/name") == name
        ):
            return dict(labels)
    raise AssertionError(f"no {name} {component} pod in the render")


def _matches(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return all(labels.get(key) == value for key, value in selector.items())


def _policy(documents: list[dict[str, Any]], component: str) -> dict[str, Any]:
    for policy in _of(documents, "NetworkPolicy"):
        if policy["metadata"]["labels"]["app.kubernetes.io/component"] == component:
            return policy
    raise AssertionError(f"no {component} NetworkPolicy")


def _peers(policy: dict[str, Any]) -> list[dict[str, Any]]:
    return [peer for rule in policy["spec"]["ingress"] for peer in rule["from"]]


def _ports(policy: dict[str, Any]) -> list[int]:
    return [port["port"] for rule in policy["spec"]["ingress"] for port in rule["ports"]]


# --- the disruption budget ---------------------------------------------------------------------


def test_the_workers_have_a_budget_by_default_that_selects_them_and_only_them() -> None:
    documents = _render()

    budgets = _of(documents, "PodDisruptionBudget")

    assert len(budgets) == 1, "one budget, the workers'; never the coordinator's"
    selector = budgets[0]["spec"]["selector"]["matchLabels"]
    assert _matches(selector, _pod_labels(documents, "worker", "trino"))
    assert not _matches(selector, _pod_labels(documents, "coordinator", "trino"))
    assert budgets[0]["spec"]["maxUnavailable"] == 1
    assert "minAvailable" not in budgets[0]["spec"]


def test_a_budget_can_be_turned_off() -> None:
    documents = _render("--set", "trino.worker.podDisruptionBudget.enabled=false")

    assert _of(documents, "PodDisruptionBudget") == []


def test_min_available_replaces_max_unavailable() -> None:
    documents = _render(
        "--set",
        "trino.worker.podDisruptionBudget.maxUnavailable=null",
        "--set",
        "trino.worker.podDisruptionBudget.minAvailable=2",
    )

    spec = _of(documents, "PodDisruptionBudget")[0]["spec"]

    assert spec["minAvailable"] == 2
    assert "maxUnavailable" not in spec


def test_both_at_once_is_refused_by_name() -> None:
    """Kubernetes refuses a budget carrying both -- at apply time, from Argo CD, after the
    render looked fine."""
    stderr = _refused("--set", "trino.worker.podDisruptionBudget.minAvailable=1")

    assert "set minAvailable or maxUnavailable, not both" in stderr


# --- the NetworkPolicy -------------------------------------------------------------------------


def test_there_is_no_policy_until_one_is_asked_for() -> None:
    """Off by default: turning it on cuts off every client the chart cannot know about."""
    assert _of(_render(), "NetworkPolicy") == []


def test_each_policy_selects_its_own_role_and_restricts_ingress_only() -> None:
    documents = _render("--set", "networkPolicy.enabled=true")

    coordinator, worker = _policy(documents, "coordinator"), _policy(documents, "worker")

    for policy, own, other in (
        (coordinator, "coordinator", "worker"),
        (worker, "worker", "coordinator"),
    ):
        selector = policy["spec"]["podSelector"]["matchLabels"]
        assert _matches(selector, _pod_labels(documents, own, "trino"))
        assert not _matches(selector, _pod_labels(documents, other, "trino"))
        # Egress is never restricted: Trino reaches what the chart cannot enumerate.
        assert policy["spec"]["policyTypes"] == ["Ingress"]


def test_the_coordinator_admits_trino_and_apchi_and_nothing_else_by_default() -> None:
    documents = _render("--set", "networkPolicy.enabled=true")

    peers = _peers(_policy(documents, "coordinator"))

    selectors = [peer["podSelector"]["matchLabels"] for peer in peers]
    assert len(peers) == 2, peers
    trino, apchi = selectors
    assert _matches(trino, _pod_labels(documents, "worker", "trino"))
    assert _matches(trino, _pod_labels(documents, "coordinator", "trino"))
    assert _matches(apchi, _pod_labels(documents, "api", "apchi"))
    assert not _matches(apchi, _pod_labels(documents, "mongodb", "apchi")), (
        "the bundled MongoDB shares Apchi's name and instance labels"
    )
    assert _ports(_policy(documents, "coordinator")) == [8080]


def test_a_worker_admits_only_trino() -> None:
    documents = _render("--set", "networkPolicy.enabled=true")

    peers = _peers(_policy(documents, "worker"))

    assert len(peers) == 1, peers
    assert _matches(
        peers[0]["podSelector"]["matchLabels"], _pod_labels(documents, "coordinator", "trino")
    )
    assert _ports(_policy(documents, "worker")) == [8080]


def test_the_https_port_is_admitted_when_trino_serves_one() -> None:
    documents = _render(
        "--set",
        "networkPolicy.enabled=true",
        "--set",
        "trino.https.enabled=true",
        "--set",
        "trino.https.existingSecret=tls",
    )

    assert _ports(_policy(documents, "coordinator")) == [8080, 8443]
    assert _ports(_policy(documents, "worker")) == [8080], "workers serve no HTTPS"


def test_a_route_admits_openshifts_router_under_either_network_plugin() -> None:
    documents = _render(
        "--set",
        "networkPolicy.enabled=true",
        "--set",
        "expose.kind=route",
        "--set",
        "expose.trino.host=trino.example.com",
    )

    namespaces = [
        peer["namespaceSelector"]["matchLabels"]
        for peer in _peers(_policy(documents, "coordinator"))
        if "namespaceSelector" in peer
    ]

    assert namespaces == [
        {"policy-group.network.openshift.io/ingress": ""},
        {"network.openshift.io/policy-group": "ingress"},
    ]
    assert not any("namespaceSelector" in p for p in _peers(_policy(documents, "worker")))


def test_an_ingress_needs_the_controllers_namespace_named() -> None:
    """Only the operator knows where their ingress controller runs, and guessing wrong would
    cut Trino off from it."""
    stderr = _refused(
        "--set",
        "networkPolicy.enabled=true",
        "--set",
        "expose.kind=ingress",
        "--set",
        "expose.trino.host=trino.example.com",
    )

    assert "networkPolicy.ingressControllerNamespaceSelector is required" in stderr


def test_an_ingress_controllers_namespace_is_admitted_once_named() -> None:
    documents = _render(
        "--set",
        "networkPolicy.enabled=true",
        "--set",
        "expose.kind=ingress",
        "--set",
        "expose.trino.host=trino.example.com",
        "--set",
        "networkPolicy.ingressControllerNamespaceSelector.kubernetes\\.io/metadata\\.name=ingress-nginx",
    )

    namespaces = [
        peer["namespaceSelector"]["matchLabels"]
        for peer in _peers(_policy(documents, "coordinator"))
        if "namespaceSelector" in peer
    ]

    assert namespaces == [{"kubernetes.io/metadata.name": "ingress-nginx"}]


def test_exposing_only_apchi_admits_no_router_to_trino() -> None:
    documents = _render(
        "--set",
        "networkPolicy.enabled=true",
        "--set",
        "expose.kind=route",
        "--set",
        "expose.apchi.host=apchi.example.com",
    )

    assert not any("namespaceSelector" in p for p in _peers(_policy(documents, "coordinator")))


@pytest.mark.parametrize("role", ["coordinator", "worker"])
def test_extra_peers_are_admitted_where_they_are_listed(tmp_path: Path, role: str) -> None:
    values = tmp_path / "values.yaml"
    peer = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "trino-gateway"}}}
    values.write_text(yaml.safe_dump({"networkPolicy": {"enabled": True, role: {"from": [peer]}}}))

    documents = _render("--values", str(values))

    assert peer in _peers(_policy(documents, role))
    other = "worker" if role == "coordinator" else "coordinator"
    assert peer not in _peers(_policy(documents, other))

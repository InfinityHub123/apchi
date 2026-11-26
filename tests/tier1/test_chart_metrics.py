"""The chart's half of #280: the setting, and a ServiceMonitor that scrapes Apchi alone."""

import subprocess
from pathlib import Path
from typing import Any

import yaml

CHART = Path(__file__).resolve().parents[2] / "charts" / "apchi"


def _render(*values: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "rel", str(CHART), "--set", "mongodb.deploy=true", *values],
        capture_output=True,
        text=True,
    )


def _objects(*values: str) -> list[dict[str, Any]]:
    rendered = _render(*values)
    assert rendered.returncode == 0, rendered.stderr
    return [doc for doc in yaml.safe_load_all(rendered.stdout) if doc]


def _apchi_env(objects: list[dict[str, Any]]) -> dict[str, str]:
    apchi = next(
        o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "rel-apchi"
    )
    container = apchi["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container["env"]}


def test_no_service_monitor_unless_asked() -> None:
    """It needs the Prometheus Operator's CRDs, which an install cannot assume."""
    objects = _objects()

    assert not [o for o in objects if o["kind"] == "ServiceMonitor"]
    assert _apchi_env(objects)["APCHI_METRICS_ENABLED"] == "true"


def test_the_service_monitor_selects_apchis_service_and_not_mongodbs() -> None:
    """The release's labels alone match MongoDB's Service too, which has no /metrics."""
    objects = _objects(
        "--set",
        "metrics.serviceMonitor.enabled=true",
        "--set",
        "metrics.serviceMonitor.labels.release=kps",
    )
    monitor = next(o for o in objects if o["kind"] == "ServiceMonitor")
    selector = monitor["spec"]["selector"]["matchLabels"]
    services = [o for o in objects if o["kind"] == "Service"]

    selected = [
        s["metadata"]["name"]
        for s in services
        if selector.items() <= s["metadata"].get("labels", {}).items()
    ]
    assert selected == ["rel-apchi"], selected
    assert monitor["metadata"]["labels"]["release"] == "kps"
    assert monitor["spec"]["endpoints"] == [{"port": "http", "path": "/metrics"}]


def test_a_service_monitor_with_metrics_off_fails_the_render() -> None:
    rendered = _render(
        "--set", "metrics.serviceMonitor.enabled=true", "--set", "metrics.enabled=false"
    )

    assert rendered.returncode != 0
    assert "needs metrics.enabled" in rendered.stderr


def test_metrics_can_be_turned_off() -> None:
    assert _apchi_env(_objects("--set", "metrics.enabled=false"))["APCHI_METRICS_ENABLED"] == (
        "false"
    )

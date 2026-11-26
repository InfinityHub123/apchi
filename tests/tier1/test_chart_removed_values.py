"""Chart values that are gone are refused by name, not ignored.

A value the chart no longer reads renders exactly as if it were absent, so an operator who
upgrades with it still set is told nothing -- and goes on believing it does something. Each
removed value fails the render with where it went, or why it went, which is one line to delete
and nothing changed on the Cluster until then.
"""

import subprocess
from pathlib import Path

import pytest

CHART = Path(__file__).resolve().parents[2] / "charts" / "apchi"


def _render(*values: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "rel", str(CHART), "--set", "mongodb.deploy=true", *values],
        capture_output=True,
        text=True,
    )


def test_the_defaults_render() -> None:
    """The refusals are about values someone set; the chart's own must never trip one."""
    rendered = _render()

    assert rendered.returncode == 0, rendered.stderr


@pytest.mark.parametrize(
    ("value", "says"),
    [
        # Removed when Apchi began rendering Trino's files (ADR-0010).
        ("trino.jvm.maxHeapSize=4G", "trino.jvm is gone"),
        ("trino.additionalCoordinatorProperties.x=y", "PUT /api/v1/admin/server-configuration"),
        ("trino.additionalWorkerProperties.x=y", "under worker"),
        # Removed when the chart stopped creating Apchi's Secrets, because Argo CD recreated
        # them on every sync (#268). `false` too: it meant "the Secrets already exist", which
        # is now the only case, and still is not something the chart reads.
        ("bootstrap.catalogs.tpch=connector.name=tpch", "Create catalogs through Apchi"),
        ("bootstrap.create=true", "bootstrap.create is gone"),
        ("bootstrap.create=false", "Apchi writes them on its first start"),
        # Removed with the high-water mark (ADR-0009).
        ("highWaterConfigMap=apchi-high-water", "highWaterConfigMap is gone"),
        ("highWaterConfigMap=", "kubectl delete configmap"),
    ],
)
def test_a_removed_value_fails_the_render_and_says_where_it_went(value: str, says: str) -> None:
    rendered = _render("--set", value)

    assert rendered.returncode != 0, f"{value} rendered as though it did nothing"
    assert says in rendered.stderr, rendered.stderr

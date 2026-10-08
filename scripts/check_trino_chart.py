"""Assert the two charts agree, and that charts/trino renders a Trino Apchi will accept.

Apchi refuses to Apply against a deployment that cannot support what it is about to do,
and every one of those preconditions exists because breaking it produces a failure that
does not look like its cause -- Apchi writes a Secret, the write succeeds, Apchi reports
success, and Trino never sees the change (§16).

A chart that ships with Apchi must not be able to break them. So this renders it and runs
Apchi's own `check()` against the result, rather than a second opinion about what the
checks mean. Run with no arguments to check; `--print` dumps the coordinator's mounts.

It caught two things while the chart was being written: a mount of the user-mapping file
under a volume name Apchi did not recognise, which Apchi reads as somebody else fighting it
over one file, and the whole class of problem that made Apchi refuse the official Trino
chart.

It also checks the two charts **agree on names**. Apchi writes these Secrets and Trino reads
them, and nothing reconciles a mismatch: Apchi would report a successful Apply the Cluster
never saw. While both lived in one chart that could not happen; now that the Secrets belong
to the Apchi chart and the mounts to the Trino one, it can, so it is checked.
"""

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from app.config import Settings
from app.pipeline.preconditions import PodSpec, PreconditionFailed, check

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "trino"
APCHI_CHART = ROOT / "charts" / "apchi"

#: Rendered with the values an Apchi install actually uses. The chart's own defaults name
#: the Deployments after the release, so Apchi's defaults are passed in rather than
#: assumed -- a chart whose names only line up under one release name is not much of a
#: guarantee.
VALUES = (
    "--set",
    "fullnameOverride=trino",
    "--set",
    "worker.replicas=1",
)


class _Cluster:
    """Only the two reads the preconditions make. Anything else is a bug in this script."""

    def __init__(self, config_maps: dict[str, Any], secrets: dict[str, Any]) -> None:
        self._config_maps = config_maps
        self._secrets = secrets

    async def read_config_map(self, name: str) -> dict[str, str] | None:
        return self._config_maps.get(name)

    async def read_secret(self, name: str) -> dict[str, str] | None:
        return self._secrets.get(name)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the preconditions must not reach the Cluster for {name!r}")


def _rendered(chart: Path, *values: str) -> list[dict[str, Any]]:
    completed = subprocess.run(
        ["helm", "template", chart.name, str(chart), *values],
        capture_output=True,
        text=True,
        check=True,
    )
    return [document for document in yaml.safe_load_all(completed.stdout) if document]


def _name_disagreements() -> list[str]:
    """Where the Trino chart mounts a Secret or volume the Apchi chart does not write.

    Compared through what each chart renders rather than by reading both values files,
    because a default that is overridden in a template is the kind of disagreement reading
    values would miss.
    """
    trino = _rendered(CHART, *VALUES)
    apchi = _rendered(APCHI_CHART, "--set", "mongodb.deploy=true")

    written = {document["metadata"]["name"] for document in apchi if document["kind"] == "Secret"}
    coordinator = _coordinator(trino)
    spec = PodSpec.model_validate(coordinator["spec"]["template"]["spec"])
    mounted = {volume.secret_name: volume.name for volume in spec.volumes if volume.secret_name}

    problems = [
        f"charts/trino mounts Secret {secret!r} (as volume {volume!r}) that charts/apchi "
        "does not create"
        for secret, volume in sorted(mounted.items())
        if secret not in written
    ]
    # The reverse is not a problem: Apchi writes two Secrets the coordinator never mounts,
    # because Apchi adds those volumes to the pod template itself when something is
    # configured -- the absence of the mount is how "none configured" is expressed.
    return problems


def _coordinator(documents: list[dict[str, Any]]) -> dict[str, Any]:
    for document in documents:
        labels = document["metadata"].get("labels") or {}
        if (
            document["kind"] == "Deployment"
            and labels.get("app.kubernetes.io/component") == "coordinator"
        ):
            return document
    raise AssertionError("the chart rendered no coordinator Deployment")


def main() -> int:
    documents = _rendered(CHART, *VALUES)
    coordinator = _coordinator(documents)
    spec = PodSpec.model_validate(coordinator["spec"]["template"]["spec"])

    if "--print" in sys.argv:
        for container in spec.every_container:
            print(container.name)
            for mount in container.volume_mounts:
                sub = f" (subPath {mount.sub_path})" if mount.sub_path else ""
                print(f"  {mount.mount_path}{sub} <- {mount.name}")
        return 0

    disagreements = _name_disagreements()
    if disagreements:
        print("The two charts do not agree:", file=sys.stderr)
        for problem in disagreements:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    cluster = _Cluster(
        {
            d["metadata"]["name"]: (d.get("data") or {})
            for d in documents
            if d["kind"] == "ConfigMap"
        },
        {
            d["metadata"]["name"]: (d.get("stringData") or {})
            for d in documents
            if d["kind"] == "Secret"
        },
    )
    try:
        asyncio.run(check(cluster, spec, Settings(_env_file=None)))  # type: ignore[arg-type]
    except PreconditionFailed as exc:
        print("Apchi would refuse the Trino this chart renders:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        f"charts/trino satisfies every precondition ({len(spec.volumes)} volumes) "
        "and agrees with charts/apchi on names"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

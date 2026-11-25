"""Assert charts/apchi renders a Trino that the Apchi it also renders will accept.

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

Apchi is checked **as the chart configures it**: its settings are built from the environment
the chart renders for the Apchi container, not from Apchi's defaults, so a value the chart
passes wrongly fails here. And the Secrets the chart creates must be the ones its coordinator
mounts -- Apchi writes them and Trino reads them, and nothing reconciles a mismatch: Apchi
would report a successful Apply the Cluster never saw.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from app.adapters.kubernetes import FORBIDDEN_CREDENTIAL_BITS, TRINO_GROUP
from app.config import Settings
from app.pipeline.preconditions import PodSpec, PreconditionFailed, check

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "apchi"

#: The release is deliberately not called `trino` or `apchi`: the names Apchi is configured
#: with must come from the chart's helpers, not from a release name that happens to match.
VALUES = ("--set", "mongodb.deploy=true", "--set", "trino.worker.replicas=1")
#: The same, with a CA bundle and a Java truststore mounted into every pod -- which adds mounts
#: to the coordinator, so the preconditions are checked against that shape as well.
TRUST_VALUES = VALUES + (
    "--set",
    "caBundle.configMap=trusted-ca",
    "--set",
    "javaTruststore.existingSecret=truststore",
)
#: Trino serving HTTPS itself and exposed by passthrough: a second port, an initContainer and
#: two more volumes on the coordinator, and a config.properties the eighth precondition reads.
HTTPS_VALUES = VALUES + (
    "--set",
    "trino.https.enabled=true",
    "--set",
    "trino.https.existingSecret=trino-server-tls",
    "--set",
    "expose.kind=route",
    "--set",
    "expose.trino.host=trino.example.com",
    "--set",
    "expose.trino.termination=passthrough",
)
RELEASE = "check"


class _Cluster:
    """Only the reads the preconditions make. Anything else is a bug in this script."""

    def __init__(
        self,
        config_maps: dict[str, Any],
        secrets: dict[str, Any],
        pod_specs: dict[str, Any],
    ) -> None:
        self._config_maps = config_maps
        self._secrets = secrets
        self._pod_specs = pod_specs

    async def read_config_map(self, name: str) -> dict[str, str] | None:
        return self._config_maps.get(name)

    async def read_secret(self, name: str) -> dict[str, str] | None:
        return self._secrets.get(name)

    async def deployment_pod_spec_if_present(self, deployment: str) -> dict[str, Any] | None:
        """The worker's template, from the same render. Served rather than refused because
        precondition 8 reads both roles (#184), and a chart whose worker leaks keys should
        fail here rather than on somebody's first Apply."""
        return self._pod_specs.get(deployment)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the preconditions must not reach the Cluster for {name!r}")


def _rendered(chart: Path, *values: str) -> list[dict[str, Any]]:
    completed = subprocess.run(
        ["helm", "template", RELEASE, str(chart), *values],
        capture_output=True,
        text=True,
        check=True,
    )
    return [document for document in yaml.safe_load_all(completed.stdout) if document]


#: Volumes holding what the operator names in values rather than what the chart creates: what
#: every pod trusts, and the certificate Trino serves HTTPS with. Their Secrets are the
#: operator's, so they are not expected among the chart's own.
OPERATORS_OWN = ("ca-bundle", "java-truststore", "https-source")


def _name_disagreements(documents: list[dict[str, Any]]) -> list[str]:
    """Where the coordinator mounts a Secret the chart does not create.

    Compared through what the chart renders rather than by reading values, because a default
    that is overridden in a template is the kind of disagreement reading values would miss.
    """
    written = {d["metadata"]["name"] for d in documents if d["kind"] == "Secret"}
    coordinator = _coordinator(documents)
    spec = PodSpec.model_validate(coordinator["spec"]["template"]["spec"])
    mounted = {
        volume.secret_name: volume.name
        for volume in spec.volumes
        if volume.secret_name and volume.name not in OPERATORS_OWN
    }

    problems = [
        f"the coordinator mounts Secret {secret!r} (as volume {volume!r}) that the chart "
        "does not create"
        for secret, volume in sorted(mounted.items())
        if secret not in written
    ]
    # The reverse is not a problem: Apchi writes two Secrets the coordinator never mounts,
    # because Apchi adds those volumes to the pod template itself when something is
    # configured -- the absence of the mount is how "none configured" is expressed.
    return problems


#: The Secret volumes that carry credentials, by volume name. The catalog seed holds every
#: catalog's `connection-password`; the certificate volume holds private keys. Named here
#: rather than inferred, because "does this volume carry a secret worth protecting" is a
#: judgement about content and not something a renderer can answer.
CREDENTIAL_VOLUMES = ("catalog-seed", "client-certificates")


def _permission_problems(documents: list[dict[str, Any]]) -> list[str]:
    """Whether every pod mounting credentials restricts them, and can still read them.

    Both halves, because each is useless alone: without `defaultMode` the kubelet writes
    0644 and the file is world-readable, and without `fsGroup` a 0640 Secret file is
    root-only and Trino -- uid 1000 -- cannot read its own private keys. A chart that
    shipped one without the other would either leak keys or fail to start. §13.2.

    Kept after §16 grew precondition 8, which refuses the same thing at runtime on both
    roles (#184), because this catches it a stage earlier and in a form that one cannot.
    It reads the chart as rendered, where a volume that asks for no mode has none -- the API
    server fills `defaultMode` in with 0644, so a pod template read back from a live cluster
    never shows the absent case. And it needs no cluster, so a chart that would leak keys
    fails CI rather than the first Apply after someone installs it.
    """
    problems: list[str] = []
    for document in documents:
        if document["kind"] != "Deployment":
            continue
        name = document["metadata"]["name"]
        spec = document["spec"]["template"]["spec"]
        volumes = {
            volume["name"]: volume for volume in spec.get("volumes") or [] if volume.get("secret")
        }
        carries = [volume for volume in CREDENTIAL_VOLUMES if volume in volumes]
        if not carries:
            continue
        if (spec.get("securityContext") or {}).get("fsGroup") != TRINO_GROUP:
            problems.append(
                f"{name} mounts {', '.join(carries)} without "
                f"securityContext.fsGroup: {TRINO_GROUP}, so a restricted file would not be "
                f"owned by a group Trino belongs to and it could not read it"
            )
        for volume in carries:
            mode = volumes[volume]["secret"].get("defaultMode")
            if mode is None:
                problems.append(
                    f"{name}'s {volume} volume sets no defaultMode, so the kubelet writes "
                    f"its files 0644 -- world-readable, and libpq refuses a key file like that"
                )
            elif mode & FORBIDDEN_CREDENTIAL_BITS:
                problems.append(
                    f"{name}'s {volume} volume has defaultMode {mode:#o}, which grants "
                    f"group-write, group-execute or world access to a credential"
                )
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


def _apchi_settings(documents: list[dict[str, Any]]) -> Settings:
    """Apchi's settings exactly as the chart configures them.

    Only plain values: one taken from the pod at runtime (the namespace) or from a Secret is
    left at Apchi's default, which is what it would read here anyway.
    """
    for document in documents:
        labels = document["metadata"].get("labels") or {}
        if document["kind"] == "Deployment" and labels.get("app.kubernetes.io/name") == "apchi":
            spec = document["spec"]["template"]["spec"]
            if spec["containers"][0]["name"] != "apchi":
                continue
            env = spec["containers"][0].get("env") or []
            # A structured setting arrives as JSON in an env var, which pydantic-settings
            # decodes from the environment but not from keyword arguments.
            values = {
                item["name"].removeprefix("APCHI_").lower(): (
                    json.loads(item["value"])
                    if item["value"].startswith(("{", "["))
                    else item["value"]
                )
                for item in env
                if "value" in item
            }
            return Settings(_env_file=None, **values)  # type: ignore[call-arg]
    raise AssertionError("the chart rendered no Apchi Deployment")


def main() -> int:
    for values in (VALUES, TRUST_VALUES, HTTPS_VALUES):
        if failed := _check(values):
            return failed
    return 0


def _check(values: tuple[str, ...]) -> int:
    documents = _rendered(CHART, *values)
    coordinator = _coordinator(documents)
    spec = PodSpec.model_validate(coordinator["spec"]["template"]["spec"])

    if "--print" in sys.argv:
        for container in spec.every_container:
            print(container.name)
            for mount in container.volume_mounts:
                sub = f" (subPath {mount.sub_path})" if mount.sub_path else ""
                print(f"  {mount.mount_path}{sub} <- {mount.name}")
        return 0

    disagreements = _name_disagreements(documents)
    if disagreements:
        print("The chart does not agree with itself:", file=sys.stderr)
        for problem in disagreements:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    exposed = _permission_problems(documents)
    if exposed:
        print("The chart would expose credentials on disk:", file=sys.stderr)
        for problem in exposed:
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
        {
            d["metadata"]["name"]: d["spec"]["template"]["spec"]
            for d in documents
            if d["kind"] == "Deployment"
        },
    )
    try:
        asyncio.run(check(cluster, spec, _apchi_settings(documents)))  # type: ignore[arg-type]
    except PreconditionFailed as exc:
        print("Apchi would refuse the Trino this chart renders:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        f"charts/apchi's Trino satisfies every precondition of the Apchi it configures "
        f"({len(spec.volumes)} volumes), restricts {len(CREDENTIAL_VOLUMES)} credential "
        "volumes, and mounts only Secrets the chart creates"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

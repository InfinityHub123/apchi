"""Assert the Helm chart renders a Deployment Apchi can run.

Two ways a chart drifts from the application, both of which only show up as a pod
that will not start:

**A name no setting has.** `APCHI_COORDINATOR_DEPLOYMENT` instead of
`APCHI_COORDINATOR_DEPLOYMENT_NAME` is silently ignored, because Settings ignores
extra environment -- so Apchi starts, reads the default, and rolls out the wrong
Deployment.

**A value no setting accepts.** Helm parses YAML numbers as float64 and renders a
large one in scientific notation: 1048576 came out as `1.048576e+06`, which Pydantic
refuses as an integer and Apchi then fails to start at all.

So this renders the chart for real and builds a Settings out of what it produced.

It also checks the one thing about the pod that is not an environment variable: the mode and
group of the TLS files Apchi mounts for itself. A private key wants both halves and each is
useless alone -- without `defaultMode` the kubelet writes 0644 and the key is world-readable,
and without a matching `fsGroup` a 0640 Secret file is root-owned and Apchi, uid 10001, cannot
read its own key. Getting that pair wrong on the Trino side took #180 through #185, so the
same pair on Apchi's own pod is asserted rather than trusted to a comment in values.

Run with no arguments to check; `--print` dumps the rendered environment.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "apchi"

#: Rendered with the dev MongoDB on, because mongodb.uri is deliberately required and
#: an unset one fails the render rather than producing a Deployment to inspect.
VALUES = ("--set", "mongodb.deploy=true")

#: And again with TLS configured to Trino and to MongoDB, because those env vars are
#: rendered only on those branches -- the default render would never see a typo in them
#: (#188, #186). The file paths are built by the template from a mount path and the Secret's
#: key names, which is exactly the kind of assembly that produces a name no setting reads.
#:
#: Both at once, not one at a time: they share the pod's `fsGroup`, so rendering them
#: separately would never catch the two volumes disagreeing about the mode.
TLS_VALUES = VALUES + (
    # An explicit URI, because mongodb.tls against the chart's own bundled MongoDB is
    # refused -- that one serves plaintext, so the files would only produce a handshake
    # nothing answers. The x509 form, which is the one the docs recommend.
    "--set",
    "mongodb.uri=mongodb://mongo-0.mongo:27017/?authMechanism=MONGODB-X509",
    "--set",
    "trino.url=https://trino:8443",
    "--set",
    "trino.tls.existingSecret=apchi-trino-tls",
    "--set",
    "trino.tls.certificateKey=tls.crt",
    "--set",
    "trino.tls.keyKey=tls.key",
    "--set",
    "mongodb.tls.existingSecret=apchi-mongo-tls",
    "--set",
    "mongodb.tls.certificateKeyKey=client.pem",
)

#: Set from the pod rather than from a value, so there is nothing to compare.
FROM_FIELD_REF = {"APCHI_KUBERNETES_NAMESPACE"}


def rendered_environment(values: tuple[str, ...] = VALUES) -> dict[str, str]:
    manifests = subprocess.run(
        ["helm", "template", "apchi", str(CHART), *values],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # Parsed without PyYAML, which is not a dependency of this project: helm can emit
    # JSON per document through its own template, but the simplest reliable route is
    # to ask kubectl-free and read the one Deployment we care about out of the YAML.
    env: dict[str, str] = {}
    name = None
    for line in manifests.splitlines():
        stripped = line.strip()
        if stripped.startswith("- name: APCHI_"):
            name = stripped.removeprefix("- name: ")
            env[name] = ""
        elif name and stripped.startswith("value: "):
            env[name] = stripped.removeprefix("value: ").strip('"')
            name = None
        elif name and stripped.startswith("valueFrom:"):
            env.pop(name)
            name = None
    return env


#: Apchi's own uid and gid, from the image's `useradd --uid 10001` -- verified by running it,
#: because Debian's useradd is free to pick a different gid and a mismatch here is a key the
#: process cannot read.
APCHI_GROUP = 10001

#: 0640, and the bits a credential must not grant. The kubelet ORs 0440 into every file in a
#: readOnly volume, so 0600 is unreachable and 0640 is the floor.
CREDENTIAL_MODE = 0o640
FORBIDDEN_BITS = 0o037


#: How many Secret volumes TLS_VALUES turns on. Asserted, so that adding a third and
#: forgetting to restrict it fails here rather than mounting a key 0644 unnoticed.
EXPECTED_TLS_VOLUMES = 2


def tls_mount_problems() -> list[str]:
    """Whether every TLS volume Apchi mounts for itself is restricted and still readable.

    Rendered with TLS on, because the volumes do not exist otherwise. Read from the template
    rather than from values, so a default that a template overrides cannot hide.
    """
    manifests = subprocess.run(
        ["helm", "template", "apchi", str(CHART), *TLS_VALUES],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    def values_of(key: str) -> list[int]:
        return [
            int(line.split(":", 1)[1].strip())
            for line in manifests.splitlines()
            if line.strip().startswith(f"{key}:")
        ]

    problems: list[str] = []
    modes = values_of("defaultMode")
    groups = values_of("fsGroup")

    if len(modes) != EXPECTED_TLS_VOLUMES:
        problems.append(
            f"{len(modes)} of the {EXPECTED_TLS_VOLUMES} TLS volumes set a defaultMode. "
            f"Without one the kubelet writes a Secret's files 0644, and a private key is "
            f"then readable by every process in the pod"
        )
    for mode in modes:
        if mode & FORBIDDEN_BITS:
            problems.append(
                f"a TLS volume has defaultMode {mode:#o}, which grants group-write, "
                f"group-execute or world access to a private key"
            )
        elif not mode & 0o040:
            problems.append(
                f"a TLS volume has defaultMode {mode:#o}, which is not group-readable -- a "
                f"Secret file is root-owned, so Apchi could not read its own key"
            )
    # One fsGroup for the pod, however many volumes hang off it. Two would mean the template
    # grew a second securityContext and the later one silently wins.
    if groups != [APCHI_GROUP]:
        problems.append(
            f"the pod renders securityContext.fsGroup {groups}, not exactly [{APCHI_GROUP}], "
            f"so a restricted file would not be owned by a group Apchi belongs to"
        )
    return problems


def main() -> int:
    from app.config import Settings

    env = rendered_environment()
    if "--print" in sys.argv:
        print(json.dumps(env, indent=2, sort_keys=True))
        return 0

    fields = {f"APCHI_{name.upper()}" for name in Settings.model_fields}
    for shape, values in (("default", VALUES), ("with TLS to Trino and MongoDB", TLS_VALUES)):
        env = rendered_environment(values)
        unknown = sorted(set(env) - fields - FROM_FIELD_REF)
        if unknown:
            print(
                f"Rendered {shape}, the chart sets environment no Settings field reads, "
                "which Apchi ignores silently and then runs on its default:",
                file=sys.stderr,
            )
            for name in unknown:
                print(f"  {name}", file=sys.stderr)
            return 1

        try:
            Settings(_env_file=None, **{name[6:].lower(): value for name, value in env.items()})
        except Exception as exc:
            print(
                f"Rendered {shape}, Apchi refuses the environment the chart produces:\n{exc}",
                file=sys.stderr,
            )
            return 1
        print(f"chart environment ok, {shape} ({len(env)} settings)")

    exposed = tls_mount_problems()
    if exposed:
        print("The chart would mount Apchi's TLS files wrongly:", file=sys.stderr)
        for problem in exposed:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        f"TLS mounts ok ({EXPECTED_TLS_VOLUMES} volumes at mode {CREDENTIAL_MODE:#o}, "
        f"fsGroup {APCHI_GROUP})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

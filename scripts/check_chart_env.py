"""Assert the Helm chart renders an environment Apchi accepts.

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

#: Set from the pod rather than from a value, so there is nothing to compare.
FROM_FIELD_REF = {"APCHI_KUBERNETES_NAMESPACE"}


def rendered_environment() -> dict[str, str]:
    manifests = subprocess.run(
        ["helm", "template", "apchi", str(CHART), *VALUES],
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


def main() -> int:
    from app.config import Settings

    env = rendered_environment()
    if "--print" in sys.argv:
        print(json.dumps(env, indent=2, sort_keys=True))
        return 0

    fields = {f"APCHI_{name.upper()}" for name in Settings.model_fields}
    unknown = sorted(set(env) - fields - FROM_FIELD_REF)
    if unknown:
        print(
            "The chart sets environment no Settings field reads, which Apchi ignores "
            "silently and then runs on its default:",
            file=sys.stderr,
        )
        for name in unknown:
            print(f"  {name}", file=sys.stderr)
        return 1

    try:
        Settings(_env_file=None, **{name[6:].lower(): value for name, value in env.items()})
    except Exception as exc:
        print(f"Apchi refuses the environment the chart renders:\n{exc}", file=sys.stderr)
        return 1

    print(f"chart environment ok ({len(env)} settings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

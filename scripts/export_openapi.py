"""Writes the OpenAPI document to openapi.json.

FastAPI generates the spec from code and has no supported spec-first workflow, so
the committed document plus a CI drift check is how a contract change is forced
into a pull request diff where a reviewer sees it.
"""

import json
import sys
from pathlib import Path

from app.main import create_app

TARGET = Path(__file__).resolve().parent.parent / "openapi.json"


def render() -> str:
    return json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    rendered = render()
    if "--check" in sys.argv:
        if not TARGET.exists():
            print(f"{TARGET.name} is missing; run scripts/export_openapi.py", file=sys.stderr)
            return 1
        if TARGET.read_text() != rendered:
            print(
                f"{TARGET.name} is out of date. The API contract changed: regenerate it "
                "and commit the result so the change is reviewed.",
                file=sys.stderr,
            )
            return 1
        return 0
    TARGET.write_text(rendered)
    print(f"wrote {TARGET.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

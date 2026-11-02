"""Put the committed API contract where the docs site can serve it.

`openapi.json` lives at the repo root because CI regenerates it there and fails on a diff,
so every contract change arrives as a reviewed diff (§19). mkdocs only serves what is inside
`docs_dir`, and a second copy committed under `docs/` would be a second thing to keep in
step -- exactly what the check at the root exists to prevent. So the one file is copied into
place as the build starts and is git-ignored.

Copied in `on_config` rather than added to `on_files`, because mkdocs runs hooks after
plugins on every event: a file this adds in `on_files` reaches the built site, but
`swagger-ui-tag` has already decided it does not exist.
"""

import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "openapi.json"
SERVED = ROOT / "docs" / "reference" / "openapi.json"


def on_config(config: Any, **kwargs: Any) -> Any:
    shutil.copyfile(CONTRACT, SERVED)
    return config

"""The system access-control file.

Only Apchi may create or drop Catalogs, and this is the file that enforces it.
`FileBasedSystemAccessControl.checkCanCreateCatalog` and `checkCanDropCatalog` gate on
the **owner** access mode, so the restriction is a `catalogs` rules block: Apchi's
identity gets `owner`, everyone else gets `all`. See section 7.1.

It is not an Operator-editable Section in slice 1. Apchi generates the whole file, and
the Permissions Section will later own the rest of it with this block still
system-owned -- visible to Operators, editable by nobody.
"""

import json
import logging
import re

from app.adapters.kubernetes import KubernetesAdapter
from app.config import Settings

logger = logging.getLogger(__name__)

RULES_KEY = "rules.json"


def render_rules(trino_user: str) -> str:
    """The whole file.

    The catch-all rule is **mandatory**, not a convenience. `canAccessCatalog` returns
    false when no rule matches, so omitting it denies every End User access to every
    catalog -- the same trap as the `queries` block in §13.4. First match wins, so
    Apchi's rule has to come first: `owner` implies `all`, and the catch-all would
    otherwise swallow it.

    The identity is anchored. Trino matches these patterns against the whole username,
    so a bare `apchi` already cannot match `notapchi`, but anchoring says so out loud --
    the cost of being wrong here is handing catalog DDL to anyone whose username
    happens to contain Apchi's.
    """
    rules = {
        "catalogs": [
            {"user": f"^{re.escape(trino_user)}$", "allow": "owner"},
            {"allow": "all"},
        ]
    }
    return json.dumps(rules, indent=2) + "\n"


async def deliver(kubernetes: KubernetesAdapter, settings: Settings) -> None:
    """Write the file to its Secret.

    Mounted as a whole volume rather than with `subPath`, so the kubelet keeps it
    current -- §16's precondition, asserted separately before every Apply. Nothing here
    waits for Trino to notice: the file's content does not change between Applies in
    slice 1, and a rules change that did need picking up is §7.5's problem.
    """
    await kubernetes.write_secret(
        settings.access_control_secret_name, {RULES_KEY: render_rules(settings.trino_user)}
    )
    logger.info("delivered the access-control rules", extra={"identity": settings.trino_user})

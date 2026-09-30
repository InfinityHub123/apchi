"""Rendering the Permissions Section to the file Trino reads.

Only Apchi may create or drop Catalogs, and this is the file that enforces it.
`FileBasedSystemAccessControl.checkCanCreateCatalog` and `checkCanDropCatalog` gate on
the **owner** access mode, so the restriction is a `catalogs` rules block: Apchi's
identity gets `owner`, everyone else gets `all`. See section 7.1.

Operator-managed grants are not here yet. What this file already carries is the block Apchi
owns and nobody may edit, and the whole file is generated -- there is no hand-written part
to preserve when the grants arrive.
"""

import json
import re

RULES_KEY = "rules.json"

#: The directory the Admin mounts the Secret at, and the file inside it. Apchi writes the
#: Secret and never touches the mount: it has to be a whole volume, because a subPath mount
#: never receives updates and Trino would read these rules once and never again (section 16).
MOUNT_DIR = "/etc/trino/access-control"
MOUNT_PATH = f"{MOUNT_DIR}/{RULES_KEY}"


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

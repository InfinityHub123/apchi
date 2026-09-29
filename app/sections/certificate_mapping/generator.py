"""Rendering the Certificate Mapping Pattern to the file Trino reads.

Trino evaluates the rules top to bottom, first match wins, and **denies authentication when
nothing matches** -- verified against a running coordinator, which answers an unmatched
principal with "No user mapping patterns match the principal". That is what makes this file
dangerous to generate carelessly, and it drives both decisions below.
"""

import json
import re
from typing import Any

from app.sections.base import Resources
from app.sections.certificate_mapping import RESOURCE

#: The key inside the Secret, and the filename.
FILE_KEY = "user-mapping.json"

#: Where Apchi puts it. The Admin points an authenticator's `user-mapping.file` at this.
MOUNT_PATH = f"/etc/trino/{FILE_KEY}"

#: Trino's behaviour with no mapping configured at all: take the name as presented. Emitted
#: when no Operator pattern is set, so that "no pattern" is a file that changes nothing
#: rather than a file that denies everyone.
_UNCHANGED = "(.*)"


def reserved_rule(trino_user: str) -> dict[str, Any]:
    """Apchi's own identity, kept working whatever the Operator writes.

    Without this the first pattern an Operator sets denies Apchi -- nothing matches, so
    authentication fails -- and Verification fails on that Apply and on every Apply after
    it, with no indication why. The same trap section 8 added the reserved verification
    identity for, in the Section that authenticates rather than authorises.
    """
    return {"pattern": f"^{re.escape(trino_user)}$", "user": trino_user}


def render_rules(desired: Resources, trino_user: str) -> str:
    """The whole file. Apchi's rule first, so no Operator pattern can shadow it."""
    rules: list[dict[str, Any]] = [reserved_rule(trino_user)]

    mapping = desired.get(RESOURCE)
    if mapping:
        rule: dict[str, Any] = {"pattern": mapping["pattern"]}
        if mapping.get("user", "$1") != "$1":
            rule["user"] = mapping["user"]
        if mapping.get("case", "keep") != "keep":
            rule["case"] = mapping["case"]
        rules.append(rule)
    else:
        rules.append({"pattern": _UNCHANGED})

    # json.dumps rather than string building: a pattern is a regex, and a regex is full of
    # backslashes that are not valid JSON escapes on their own. Hand-built JSON produced a
    # file Trino rejected outright.
    return json.dumps({"rules": rules}, indent=2) + "\n"

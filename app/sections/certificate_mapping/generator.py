"""Rendering the Certificate Mapping Pattern to the file Trino reads.

Trino evaluates the rules top to bottom, first match wins, and **denies authentication when
nothing matches** -- verified against a running coordinator, which answers an unmatched
principal with "No user mapping patterns match the principal". That is what makes this file
dangerous to generate carelessly, and it drives both decisions below.
"""

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.sections.base import Resources
from app.sections.certificate_mapping import RESOURCE
from app.sections.certificate_mapping.model import CertificateMappingWrite

#: The key inside the Secret, and the filename.
FILE_KEY = "user-mapping.json"

#: Where Apchi puts it. The Admin points an authenticator's `user-mapping.file` at this.
MOUNT_PATH = f"/etc/trino/{FILE_KEY}"

#: Trino's behaviour with no mapping configured at all: take the name as presented. Always
#: last, so a principal no pattern matches keeps the name it presented instead of being
#: refused. Apchi denies nobody: what a caller may do is Permissions' answer (§13.4), and an
#: authentication file that silently became an authorisation one would be the wrong place to
#: decide it.
_UNCHANGED = "(.*)"


def reserved_rule(trino_user: str) -> dict[str, Any]:
    """Apchi's own identity, kept working whatever the Operator writes.

    Without this the first pattern an Operator sets denies Apchi -- nothing matches, so
    authentication fails -- and Verification fails on that Apply and on every Apply after
    it, with no indication why. The same trap section 8 added the reserved verification
    identity for, in the Section that authenticates rather than authorises.
    """
    return {"pattern": f"^{re.escape(trino_user)}$", "user": trino_user}


def _rule(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """One pattern as Trino wants it, with the defaults left out.

    `user` defaults to the first capturing group and `case` to leaving the name alone, so
    writing them out would add noise to a file an Admin may well have to read during an
    incident.
    """
    rule: dict[str, Any] = {"pattern": mapping["pattern"]}
    if mapping.get("user", "$1") != "$1":
        rule["user"] = mapping["user"]
    if mapping.get("case", "keep") != "keep":
        rule["case"] = mapping["case"]
    return rule


def render_rules(
    desired: Resources, trino_user: str, preserved: Sequence[CertificateMappingWrite] = ()
) -> str:
    """The whole file, in the order first-match-wins makes meaningful.

    Apchi's own rule first, so no pattern can shadow it. Then the Operator's pattern, which
    is the convention a Cluster is migrating *to* -- ahead of the preserved ones, so a
    subject matching both resolves to the destination rather than the origin. Then the
    patterns an Admin is preserving through that migration (§13.3). Then the catch-all.
    """
    rules: list[dict[str, Any]] = [reserved_rule(trino_user)]

    mapping = desired.get(RESOURCE)
    if mapping:
        rules.append(_rule(mapping))
    rules.extend(_rule(pattern.model_dump(mode="json")) for pattern in preserved)
    rules.append({"pattern": _UNCHANGED})

    # json.dumps rather than string building: a pattern is a regex, and a regex is full of
    # backslashes that are not valid JSON escapes on their own. Hand-built JSON produced a
    # file Trino rejected outright.
    return json.dumps({"rules": rules}, indent=2) + "\n"

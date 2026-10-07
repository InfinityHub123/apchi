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


class Unreadable(Exception):
    """A file this module cannot read. Translated to the Section contract's ParseProblem by
    the Section, so the generator keeps knowing nothing about the pipeline."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


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


def parse_rules(
    path: str, content: str, trino_user: str
) -> tuple[Resources, list[tuple[str, Any]]]:
    """The inverse of `render_rules`: the file back into the Operator's one pattern.

    Returns the resources and whatever could not be accounted for, as (description, content)
    pairs for the caller to turn into `Unaccounted`.

    Two of the rules in a file Apchi wrote are Apchi's own rather than an Operator's
    pattern: the reserved rule keeping Apchi's identity working, and the trailing catch-all
    that stops an unmatched principal being refused. Both are dropped -- on a Cluster that
    already ran Apchi, importing them as Operator configuration would mean generating each
    of them a second time.

    What is left is where this Section's model is smaller than Trino's file. Apchi holds
    **one** pattern (§13.3) and a hand-written file may have many, so the first is the
    Operator's and the rest are unaccounted for -- which is the right answer rather than a
    limitation: §13.3 keeps a Cluster's existing patterns as Admin values beneath the single
    pattern it is migrating to, and these are those patterns.
    """
    try:
        document = json.loads(content)
    except json.JSONDecodeError as exc:
        raise Unreadable(path, f"not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise Unreadable(path, "the file is not a JSON object")
    rules = document.get("rules")
    if not isinstance(rules, list):
        raise Unreadable(path, "there is no 'rules' array")

    unaccounted: list[tuple[str, Any]] = [
        (f"{key!r} outside the rules array", value)
        for key, value in sorted(document.items())
        if key != "rules"
    ]

    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise Unreadable(path, f"rule {index} is not an object")

    # Positionally, exactly where render_rules puts them, rather than by matching anywhere
    # in the list. `(.*)` is a legal Operator pattern -- it matches every subject and leaves
    # the name as presented -- and matching it anywhere made an Operator who wrote it
    # disappear on the way back in.
    body: list[tuple[int, dict[str, Any]]] = list(enumerate(rules))
    if body and body[0][1] == reserved_rule(trino_user):
        body = body[1:]
    if body and body[-1][1] == {"pattern": _UNCHANGED}:
        body = body[:-1]

    operator: dict[str, Any] | None = None
    for index, rule in body:
        extra = sorted(set(rule) - {"pattern", "user", "case"})
        if extra or "pattern" not in rule:
            # `allow` is the one Trino supports and Apchi does not expose, because a single
            # rule that denies is a Cluster nobody can authenticate to (model.py).
            unaccounted.append((f"rule {index} uses {', '.join(extra) or 'no pattern'}", rule))
            continue
        if operator is None:
            operator = rule
            continue
        unaccounted.append((f"rule {index} is a further pattern", rule))

    resources: Resources = {}
    if operator is not None:
        resources[RESOURCE] = CertificateMappingWrite.model_validate(operator).model_dump(
            mode="json"
        )
    return resources, unaccounted

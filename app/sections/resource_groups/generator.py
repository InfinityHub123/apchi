"""Rendering the Resource Groups Section to the two files Trino reads.

The JSON is a tree; the Candidate holds a flat map of paths. Rebuilding the tree here is
what lets Review say which group changed: a nested document diffs as one blob, and an
Operator asking what an Apply does to `global.etl` would be told that the configuration
changed.

The properties file exists because the JSON is inert without it -- `resource-groups.json`
is read only because `resource-groups.configuration-manager` points at it, and that property
is rejected outright in `config.properties`.
"""

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic.alias_generators import to_camel

from app.sections.base import Resources
from app.sections.resource_groups import SELECTORS, SEPARATOR, SETTINGS
from app.sections.resource_groups.model import (
    ResourceGroupSettings,
    ResourceGroupWrite,
    Selector,
)

#: The keys inside the Secret, and the filenames.
RULES_KEY = "resource-groups.json"
MANAGER_KEY = "resource-groups.properties"

RULES_PATH = f"/etc/trino/{RULES_KEY}"
MANAGER_PATH = f"/etc/trino/{MANAGER_KEY}"

#: Read as `etc/resource-groups.properties`, so it cannot be merged into config.properties:
#: `resource-groups.configuration-manager` there fails startup with "Configuration property
#: 'resource-groups.configuration-manager' was not used".
MANAGER_FILE = (
    f"resource-groups.configuration-manager=file\nresource-groups.config-file={RULES_PATH}\n"
)


#: The keys that are not groups. Reserved by a character a group name may not contain.
_RESERVED = (SELECTORS, SETTINGS)


def groups_of(stored: Resources) -> dict[str, dict[str, Any]]:
    """The groups, by path. Everything but the reserved keys."""
    return {path: value for path, value in stored.items() if path not in _RESERVED}


def settings_of(stored: Resources) -> ResourceGroupSettings:
    return ResourceGroupSettings.model_validate(stored.get(SETTINGS, {}))


def selectors_of(stored: Resources) -> list[dict[str, Any]]:
    selectors: list[dict[str, Any]] = stored.get(SELECTORS, [])
    return selectors


def parent_of(path: str) -> str | None:
    """The path one level up, or None for a root group."""
    head, separator, _ = path.rpartition(SEPARATOR)
    return head if separator else None


def children_of(paths: list[str], path: str) -> list[str]:
    return [other for other in paths if parent_of(other) == path]


def _trino(model: BaseModel) -> dict[str, Any]:
    """A model as Trino spells it: the fields that are set, camelCased.

    A rule rather than a table, so a field added to the model cannot be forgotten here.
    """
    dumped = model.model_dump(mode="json", exclude_none=True)
    return {to_camel(name): value for name, value in dumped.items()}


def _spec(path: str, stored: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    """One group as Trino wants it: its own fields, then its children beneath it."""
    spec: dict[str, Any] = {"name": path.rpartition(SEPARATOR)[2]}
    spec.update(_trino(ResourceGroupWrite.model_validate(stored[path])))
    subgroups = [_spec(child, stored, paths) for child in sorted(children_of(paths, path))]
    if subgroups:
        spec["subGroups"] = subgroups
    return spec


def render_rules(desired: Resources) -> str:
    """The whole tree, rebuilt from the flat paths, with the selectors in the order given."""
    groups = groups_of(desired)
    paths = sorted(groups)
    roots = [_spec(path, groups, paths) for path in paths if parent_of(path) is None]
    selectors = [_trino(Selector.model_validate(selector)) for selector in selectors_of(desired)]
    document: dict[str, Any] = {}
    # Before the groups, because it governs them -- and because a person reading this file
    # during an incident should meet it first.
    document.update(_trino(settings_of(desired)))
    document["rootGroups"] = roots
    document["selectors"] = selectors
    return json.dumps(document, indent=2) + "\n"


def render(desired: Resources) -> dict[str, str]:
    """Both files, or neither.

    An empty Section takes the manager away with the rules. Leaving the properties file
    behind would point Trino at a file that is no longer mounted, and Trino refuses to start
    when a file it was told to read is missing.
    """
    if not groups_of(desired) and not selectors_of(desired):
        return {}
    return {RULES_PATH: render_rules(desired), MANAGER_PATH: MANAGER_FILE}


class Unreadable(Exception):
    """A file this module cannot read. Translated by the Section, so the generator keeps
    knowing nothing about the pipeline."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


def _snake(camel: str) -> str:
    """Trino's spelling back to the model's. The inverse of `to_camel`, and written out
    rather than imported because Pydantic ships the one direction."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()


def _fields(model: type[BaseModel]) -> set[str]:
    return set(model.model_fields)


def _flatten(
    spec: Any,
    parent: str | None,
    path: str,
    groups: dict[str, dict[str, Any]],
    unaccounted: list[tuple[str, Any]],
) -> None:
    """One group and its subgroups, into the flat map the Candidate holds.

    The tree exists only in the file; the Candidate is flat so Review can name the group
    that changed. Rebuilding the path as the walk descends is what makes `global.etl` out of
    an `etl` nested under a `global`.
    """
    if not isinstance(spec, dict):
        raise Unreadable(path, f"a group under {parent or 'the root'} is not an object")
    name = spec.get("name")
    if not isinstance(name, str) or not name:
        raise Unreadable(path, f"a group under {parent or 'the root'} has no name")
    full = f"{parent}{SEPARATOR}{name}" if parent else name

    known = _fields(ResourceGroupWrite)
    own: dict[str, Any] = {}
    for key, value in spec.items():
        if key in ("name", "subGroups"):
            continue
        field = _snake(key)
        if field not in known:
            # Trino has settings Apchi's model does not carry, and a hand-written file is
            # where they turn up. Reported against the group so an Operator knows which one.
            unaccounted.append((f"group {full!r} sets {key!r}", {key: value}))
            continue
        own[field] = value

    try:
        groups[full] = ResourceGroupWrite.model_validate(own).model_dump(mode="json")
    except ValidationError as exc:
        raise Unreadable(path, f"group {full!r} is not valid: {exc.errors()[0]['msg']}") from exc

    subgroups = spec.get("subGroups", [])
    if not isinstance(subgroups, list):
        raise Unreadable(path, f"group {full!r} has a subGroups that is not a list")
    for child in subgroups:
        _flatten(child, full, path, groups, unaccounted)


def parse_rules(path: str, content: str) -> tuple[Resources, list[tuple[str, Any]]]:
    """The inverse of `render_rules`: the tree flattened back to paths, selectors in order.

    Selector order is the configuration -- first match wins -- so it is preserved exactly.
    Group order is not, and the groups come back sorted by path, which is what `render_rules`
    writes anyway.
    """
    try:
        document = json.loads(content)
    except json.JSONDecodeError as exc:
        raise Unreadable(path, f"not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise Unreadable(path, "the file is not a JSON object")

    unaccounted: list[tuple[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}
    roots = document.get("rootGroups", [])
    if not isinstance(roots, list):
        raise Unreadable(path, "rootGroups is not a list")
    for root in roots:
        _flatten(root, None, path, groups, unaccounted)

    raw_selectors = document.get("selectors", [])
    if not isinstance(raw_selectors, list):
        raise Unreadable(path, "selectors is not a list")
    selectors: list[dict[str, Any]] = []
    known = _fields(Selector)
    for index, raw in enumerate(raw_selectors):
        if not isinstance(raw, dict):
            raise Unreadable(path, f"selector {index} is not an object")
        fields: dict[str, Any] = {}
        for key, value in raw.items():
            field = _snake(key)
            if field not in known:
                unaccounted.append((f"selector {index} matches on {key!r}", {key: value}))
                continue
            fields[field] = value
        try:
            selectors.append(Selector.model_validate(fields).model_dump(mode="json"))
        except ValidationError as exc:
            raise Unreadable(
                path, f"selector {index} is not valid: {exc.errors()[0]['msg']}"
            ) from exc

    settings_fields = {
        _snake(key): value
        for key, value in document.items()
        if _snake(key) in _fields(ResourceGroupSettings)
    }
    for key in document:
        if key not in ("rootGroups", "selectors") and _snake(key) not in _fields(
            ResourceGroupSettings
        ):
            unaccounted.append((f"the file sets {key!r}", {key: document[key]}))

    resources: Resources = dict(groups)
    resources[SETTINGS] = ResourceGroupSettings.model_validate(settings_fields).model_dump(
        mode="json"
    )
    resources[SELECTORS] = selectors
    return resources, unaccounted

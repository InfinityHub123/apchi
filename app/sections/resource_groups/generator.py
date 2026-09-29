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
from typing import Any

from pydantic import BaseModel
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

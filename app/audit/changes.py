"""What changed, derived rather than declared.

Two jobs, both about not trusting a route to say the right thing about itself.

**The Candidate diff.** Comparing the stored Candidate with the one about to replace it yields
§20's category, resource and action for free -- so a Section added later is audited without
anybody touching the audit code, and a route cannot describe its own change wrongly because it
never describes it at all.

**The category of a refused request.** A refusal never reaches the Candidate, so there is no
diff to derive anything from; all that is left is the route. Routers already declare a tag,
and for the six Sections that tag *is* the category under one mechanical rule. The rest is a
short table, and `tests/tier1/test_audit_candidate.py` fails when a tag is missing from it --
so a router added later cannot land in a default bucket quietly.
"""

import json
from dataclasses import dataclass
from typing import Any, Final

from app.audit.events import Action, Category
from app.sections import SectionName

#: The six, as identifiers. Written out rather than derived from `SectionName`, because
#: `typing.get_args` on a Literal returns `tuple[Any, ...]` and the point of this tuple is to
#: be the checked way back *to* that Literal. `test_audit_candidate.py` asserts the two agree,
#: so a seventh Section cannot be added without this noticing.
_SECTIONS: Final[tuple[SectionName, ...]] = (
    "catalogs",
    "client_certificates",
    "certificate_mapping",
    "permissions",
    "resource_groups",
    "event_listeners",
)

#: Tags that are not one of the six Sections. `pipeline` for the Candidate-wide operations,
#: `admin` for the surfaces that are not a Section at all (§3, §14) -- including tokens, which
#: mint a credential that acts as its owner.
_BY_TAG: Final[dict[str, Category]] = {
    "admin": "admin",
    "adoption": "admin",
    "tokens": "admin",
    "candidate": "pipeline",
    "applies": "pipeline",
    "validations": "pipeline",
    "snapshots": "pipeline",
}

#: Tags on routes that change nothing auditable. `auth` is a sign-in, which is a credential
#: event the log already carries and not a configuration change; `health` and `audit` are
#: reads. Listed rather than defaulted, so the test that walks the contract can tell "not
#: auditable" from "nobody has decided yet".
NOT_AUDITED: Final[frozenset[str]] = frozenset({"auth", "health", "audit"})


def as_section(name: str) -> SectionName | None:
    """A string narrowed to one of the six, or None.

    A `Literal` cannot be narrowed by a membership test, so this is the one checked crossing
    between an arbitrary string and a Section name.
    """
    for section in _SECTIONS:
        if section == name:
            return section
    return None


def category_of(tags: list[str]) -> Category | None:
    """The category a route's changes belong to, or None when it changes nothing auditable.

    The six Sections are their own tag with spaces for underscores -- "client certificates"
    reads better in the published contract than `client_certificates`, and that is the only
    difference between the two.
    """
    for tag in tags:
        if tag in NOT_AUDITED:
            return None
        if (section := as_section(tag.replace(" ", "_"))) is not None:
            return section
        if tag in _BY_TAG:
            return _BY_TAG[tag]
    return None


@dataclass(frozen=True)
class Change:
    """One resource that moved, and which way."""

    category: Category
    resource: str
    action: Action


Sections = dict[str, dict[str, Any]]


def _same(left: Any, right: Any) -> bool:
    """Equal content, whatever order it is written in.

    `sort_keys` because a resource re-serialised with its keys in another order is the same
    resource: without it every Apply would appear to have edited everything. Both sides reach
    here already dumped by the same Pydantic model, so there is no BSON/JSON mismatch left to
    normalise -- a datetime is an ISO string on both.
    """
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def changes_between(before: Sections, after: Sections) -> list[Change]:
    """Every resource that was added, removed or edited, Section by Section.

    Sorted, so one request that touched several resources produces a stable order rather than
    whatever the dictionaries happened to iterate in.
    """
    changes: list[Change] = []
    for name in sorted(set(before) | set(after)):
        category = as_section(name)
        if category is None:
            # A key that is not one of the six. A Candidate that somehow carries one is not
            # something to attribute a change to, and dropping it here keeps the diff honest.
            continue
        was, now = before.get(name, {}), after.get(name, {})
        for resource in sorted(set(was) | set(now)):
            if resource not in was:
                changes.append(Change(category, resource, "created"))
            elif resource not in now:
                changes.append(Change(category, resource, "removed"))
            elif not _same(was[resource], now[resource]):
                changes.append(Change(category, resource, "updated"))
    return changes

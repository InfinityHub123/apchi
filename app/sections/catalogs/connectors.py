"""The curated connector schemas, and what Apchi does with a connector it has no schema
for.

Apchi curates the connectors Operators actually use so a typo like `connection-uri` for
`connection-url` fails at request time rather than at Apply. Connectors outside that set
pass through and are marked unsupported in the API, validated only by the ephemeral Trino
at Apply.

Being uncurated is a different thing from not existing, and this module now answers both.
`postgresqlaa` used to be accepted -- the model constrains the name to a regex, and anything
uncurated passes through -- so a typo in the one field that decides which plugin loads was
found minutes later by the probe. The list of connectors that *do* exist comes from the
coordinator rather than from anything written here; `Trino.connector_factories` explains why
that is the only trustworthy source.
"""

import difflib
from collections.abc import Collection
from typing import Any

from app.sections.catalogs.schemas import CURATED_SCHEMAS
from app.sections.properties import PropertyProblem, validate

__all__ = [
    "PropertyProblem",
    "is_curated",
    "unknown_connector_problem",
    "validate_properties",
]

#: How close a near miss has to be before it is offered as a suggestion. 0.8 keeps
#: `postgresqlaa` -> `postgresql` and drops the pairs that would read as noise; `mysql` and
#: `mariadb` score far below it, which is the point -- suggesting one for the other would be
#: worse than suggesting nothing.
_SIMILAR_ENOUGH = 0.8


def is_curated(connector: str) -> bool:
    return connector in CURATED_SCHEMAS


def validate_properties(connector: str, properties: dict[str, Any]) -> dict[str, str]:
    return validate(CURATED_SCHEMAS.get(connector), properties, connector, "connector")


def unknown_connector_problem(connector: str, available: Collection[str] | None) -> str | None:
    """Why this connector cannot work, or None if nothing can be said against it.

    `available` of None means the coordinator would not say what it has, and then **nothing
    is refused**. The check rests on parsing a sentence out of a Trino error message, which
    is text no contract covers, so the way it fails has to be "Apchi stops offering an
    opinion" and never "Apchi refuses every Catalog". The probe at Validation is still there
    to catch what this one lets past.
    """
    if available is None or connector in available:
        return None
    near = difflib.get_close_matches(connector, sorted(available), n=1, cutoff=_SIMILAR_ENOUGH)
    suggestion = f" Did you mean {near[0]!r}?" if near else ""
    return (
        f"This Trino cluster has no connector named {connector!r}, so a Catalog using it "
        f"could never load.{suggestion}"
    )

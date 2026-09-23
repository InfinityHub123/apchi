"""Validating catalog properties against a curated schema, or passing them through.

Apchi curates the connectors Operators actually use so a typo like `connection-uri`
for `connection-url` fails at request time rather than at Apply. Connectors outside
that set pass through and are marked unsupported in the API, validated only by the
ephemeral Trino at Apply.
"""

import difflib
from typing import Any

from app.sections.catalogs.schemas import CURATED_SCHEMAS, ConnectorSchema


class PropertyProblem(Exception):
    """One or more properties do not fit the connector's schema."""

    def __init__(self, problems: list[dict[str, str]]) -> None:
        super().__init__("; ".join(p["problem"] for p in problems))
        self.problems = problems


def is_curated(connector: str) -> bool:
    return connector in CURATED_SCHEMAS


def _required_for(schema: ConnectorSchema, properties: dict[str, str]) -> set[str]:
    """Required names, including those a selector property brings into play."""
    required = set(schema.required)
    for branch in schema.branches:
        chosen = properties.get(branch.selector, branch.default)
        if chosen is None:
            continue
        required |= set(branch.values.get(chosen, frozenset()))
    return required


def _selector_problems(schema: ConnectorSchema, properties: dict[str, str]) -> list[dict[str, str]]:
    problems = []
    for branch in schema.branches:
        value = properties.get(branch.selector)
        if value is None or not branch.allowed:
            continue
        if value not in branch.allowed:
            problems.append(
                {
                    "property": branch.selector,
                    "problem": (
                        f"{value!r} is not a valid value; expected one of "
                        f"{', '.join(sorted(branch.allowed))}"
                    ),
                }
            )
    return problems


def validate_properties(connector: str, properties: dict[str, Any]) -> dict[str, str]:
    """Returns the properties to store.

    Raises PropertyProblem for a curated connector whose properties do not fit.
    Pass-through connectors are returned unchanged.
    """
    flattened = {str(k): str(v) for k, v in properties.items()}
    schema = CURATED_SCHEMAS.get(connector)
    if schema is None:
        return flattened

    problems: list[dict[str, str]] = _selector_problems(schema, flattened)

    known = schema.known()
    for name in sorted(flattened):
        if name in known:
            continue
        # A typo is the common case, so say what was probably meant. Without this
        # the Operator gets "unknown property" and has to go read the Trino docs.
        suggestion = difflib.get_close_matches(name, known, n=1, cutoff=0.8)
        hint = f"; did you mean {suggestion[0]!r}?" if suggestion else ""
        problems.append(
            {
                "property": name,
                "problem": f"{name!r} is not a property of the {connector!r} connector{hint}",
            }
        )

    for name in sorted(_required_for(schema, flattened) - set(flattened)):
        problems.append(
            {"property": name, "problem": f"{name!r} is required by the {connector!r} connector"}
        )

    if problems:
        raise PropertyProblem(problems)
    return flattened

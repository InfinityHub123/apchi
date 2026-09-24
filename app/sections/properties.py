"""Validating resource properties against a curated schema, or passing them through.

Catalogs and Event Listeners have the same problem: their properties are defined by
whichever Trino plugin implements them, so Apchi curates the ones Operators actually use
and passes the rest through. This is that machinery, with the schema and the noun for
what is being validated supplied by the Section.
"""

import difflib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


class PropertyProblem(Exception):
    """One or more properties do not fit the schema."""

    def __init__(self, problems: list[dict[str, str]]) -> None:
        super().__init__("; ".join(p["problem"] for p in problems))
        self.problems = problems


@dataclass(frozen=True)
class Branch:
    """A selector property whose value decides what else is required."""

    selector: str
    values: Mapping[str, frozenset[str]]
    default: str | None = None
    #: Values the selector itself accepts. Empty means "any value in `values`".
    allowed: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PropertySchema:
    required: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()
    branches: tuple[Branch, ...] = ()
    #: Properties whose value is open-ended: present, but not otherwise constrained.
    open_ended: frozenset[str] = frozenset()
    prefixes: tuple[str, ...] = field(default=())

    def known(self) -> frozenset[str]:
        names = set(self.required) | set(self.optional) | set(self.open_ended)
        for branch in self.branches:
            names.add(branch.selector)
            for required in branch.values.values():
                names |= required
        return frozenset(names)


def _required_for(schema: PropertySchema, properties: dict[str, str]) -> set[str]:
    """Required names, including those a selector property brings into play."""
    required = set(schema.required)
    for branch in schema.branches:
        chosen = properties.get(branch.selector, branch.default)
        if chosen is None:
            continue
        required |= set(branch.values.get(chosen, frozenset()))
    return required


def _selector_problems(schema: PropertySchema, properties: dict[str, str]) -> list[dict[str, str]]:
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


def validate(
    schema: PropertySchema | None, properties: dict[str, Any], subject: str, kind: str
) -> dict[str, str]:
    """Returns the properties to store.

    Raises PropertyProblem when a curated schema does not fit. No schema means
    pass-through: the properties are returned unchanged, and Trino is the first thing
    that will check them.

    `subject` and `kind` are only for the messages -- "the 'postgresql' connector", "the
    'kafka' event listener" -- because an Operator reading an error needs to know which
    thing rejected their property.
    """
    flattened = {str(k): str(v) for k, v in properties.items()}
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
                "problem": f"{name!r} is not a property of the {subject!r} {kind}{hint}",
            }
        )

    for name in sorted(_required_for(schema, flattened) - set(flattened)):
        problems.append(
            {"property": name, "problem": f"{name!r} is required by the {subject!r} {kind}"}
        )

    if problems:
        raise PropertyProblem(problems)
    return flattened

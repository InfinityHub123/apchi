"""Validating resource properties against a curated schema, or passing them through.

Catalogs and Event Listeners have the same problem: their properties are defined by
whichever Trino plugin implements them, so Apchi curates the ones Operators actually use
and passes the rest through. This is that machinery, with the schema and the noun for
what is being validated supplied by the Section.
"""

import difflib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
    #: Which of the above carry a credential, marked here rather than guessed from the name.
    #:
    #: A name test cannot do this job. `mongodb.connection-url` embeds the password and the
    #: connector has no separate one, so nothing in its name says credential; a set of
    #: HTTP headers can carry a bearer token; and `s3.aws-access-key` reads exactly like a
    #: secret and is an identifier that travels in every signed request. The log redaction
    #: filter in `app.logging` is a name test, and it is deliberately not this: it errs
    #: towards hiding too much, which is right for a log file and wrong for a form that has
    #: to tell somebody which field to put a password in.
    secrets: frozenset[str] = frozenset()

    def known(self) -> frozenset[str]:
        names = set(self.required) | set(self.optional) | set(self.open_ended)
        for branch in self.branches:
            names.add(branch.selector)
            for required in branch.values.values():
                names |= required
        return frozenset(names)


class BranchRules(BaseModel):
    """One selector property, and what each of its values brings with it."""

    model_config = ConfigDict(extra="forbid")

    selector: str = Field(description="The property whose value decides the rest.")
    default: str | None = Field(
        default=None,
        description=(
            "What Apchi assumes when the selector is absent, so the properties its value "
            "requires are required too. Null means a missing selector brings nothing."
        ),
    )
    allowed: list[str] | None = Field(
        description=(
            "The values the selector accepts. **Null means any value is accepted** -- the "
            "names in `values` are then the ones Apchi knows what to do with rather than "
            "the only ones permitted, which is the difference between a closed list and a "
            "suggestion."
        )
    )
    values: dict[str, list[str]] = Field(
        description=(
            "Per selector value, the properties that become required. A value mapping to an "
            "empty list is a real answer: that branch requires nothing further."
        )
    )


class PropertyRules(BaseModel):
    """What Apchi will accept for one curated connector or Event Listener type.

    The same rules the write endpoints enforce, so a form built from this is refused for the
    same reasons and no others. Everything is sorted, because the underlying sets are
    unordered and a response that reshuffled between calls would be a committed contract that
    cannot be diffed.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    required: list[str]
    optional: list[str]
    branches: list[BranchRules]
    open_ended: list[str] = Field(
        description=(
            "Properties whose value Apchi does not constrain. Present and accepted; what is "
            "in them is Trino's business, which is also why nothing here can redact them."
        )
    )
    prefixes: list[str] = Field(
        description="Any property starting with one of these is accepted, name unchecked."
    )
    secrets: list[str] = Field(
        description=(
            "Which of the properties above carry a credential. Marked per connector rather "
            "than inferred from the name, because the name does not always say: "
            "`mongodb.connection-url` embeds the password and the connector has no separate "
            "one, while `s3.aws-access-key` reads like a secret and is an identifier. §12 "
            "makes all Apchi users trusted, so this is not an access rule -- it is what a "
            "form needs to know to mark a field, and what a diff needs to avoid putting a "
            "password on a screen somebody is sharing."
        )
    )


def describe(name: str, schema: PropertySchema) -> PropertyRules:
    """One curated schema, as the API returns it.

    Exhaustive over `PropertySchema`'s fields by construction, and a test asserts that --
    a field added to the schema and not to this projection would be a rule the write
    endpoints enforce and no client can discover.
    """
    return PropertyRules(
        name=name,
        required=sorted(schema.required),
        optional=sorted(schema.optional),
        branches=[
            BranchRules(
                selector=branch.selector,
                default=branch.default,
                # Empty means "unconstrained" inside the validator, which is not what an
                # empty list says to a client reading JSON. Null says it.
                allowed=sorted(branch.allowed) if branch.allowed else None,
                values={value: sorted(names) for value, names in sorted(branch.values.items())},
            )
            for branch in schema.branches
        ],
        open_ended=sorted(schema.open_ended),
        prefixes=sorted(schema.prefixes),
        secrets=sorted(schema.secrets),
    )


#: Every field of `PropertySchema`, so a test can assert `describe` carries all of them.
SCHEMA_FIELDS = tuple(f.name for f in fields(PropertySchema))


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

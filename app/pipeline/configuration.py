"""A configuration as one portable document, and what it takes to read one back in.

What this is for is portability. An Operator has a configuration they like -- on another
Cluster, from last month, from a colleague -- and wants it here to edit. It is not a backup:
keeping MongoDB is the platform's job, not Apchi's (#110), and nothing here is a road back
from losing it.

Two scopes share this one format, because the only difference between them is the Admin
values. Admin values are applied through the pipeline and never recorded in a Snapshot
(invariant 9, §14), so a Snapshot holds less than the Cluster is running -- and an Operator
cannot set them, so an Operator's document must not carry them and an Operator's import must
not quietly apply them.

**A document holds real credentials.** Snapshots store actual secret values rather than
references (§12), so every connection password and every private key an exported
configuration uses is in the file, in the clear. That is what makes an imported
configuration work rather than need re-entering, and it is the whole of what has to be said
about handling the file: treat it the way a password database is treated.
"""

import logging
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.api.errors import UnprocessablePayload
from app.sections import SectionName
from app.sections.admin import AdminValues
from app.sections.base import Section, ValidationFailure
from app.sections.registry import REGISTERED, SECTIONS
from app.version import VERSION

logger = logging.getLogger(__name__)

#: Identifies the document, so a reader can refuse a file that is not one of these rather
#: than discovering it halfway through. Versioned for the same reason.
FORMAT: Final = "apchi.configuration/v1"

#: One Section's resources, exactly as a Snapshot and the Candidate store them.
Sections = dict[SectionName, dict[str, Any]]


class Provenance(BaseModel):
    """Where a document came from. Informational, and it never gates an import.

    Importing another Cluster's configuration is the point of the feature, so a document
    whose provenance does not match this Cluster is the normal case rather than an error.
    #113 carried a cluster id for the opposite purpose -- refusing a foreign file -- and it
    has already been removed.
    """

    exported_at: datetime
    apchi_version: str = Field(description="The Apchi that wrote this document.")
    source: Literal["snapshot", "candidate"] = Field(
        description="Whether this was exported from a committed Snapshot or from staged work."
    )
    snapshot: int | None = Field(
        default=None,
        description="The Snapshot this came from, when it came from one.",
    )


class Configuration(BaseModel):
    """The six Sections, and the Admin values when an Admin exported it."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["apchi.configuration/v1"] = FORMAT
    provenance: Provenance
    sections: Sections
    admin: AdminValues | None = Field(
        default=None,
        description=(
            "Present only in an Admin export. The Sections above are everything an Operator "
            "owns; these are what makes the document the whole of what the Cluster runs."
        ),
    )


def _provenance(source: Literal["snapshot", "candidate"], snapshot: int | None) -> Provenance:
    return Provenance(
        exported_at=datetime.now(UTC),
        apchi_version=VERSION,
        source=source,
        snapshot=snapshot,
    )


def of_snapshot(number: int, sections: Sections) -> Configuration:
    logger.info("configuration exported", extra={"source": "snapshot", "snapshot": number})
    return Configuration(provenance=_provenance("snapshot", number), sections=sections)


def of_candidate(base_snapshot: int | None, sections: Sections) -> Configuration:
    """Staged work, which is in no Snapshot and is the case "I was working on this and want
    it elsewhere" actually needs."""
    logger.info("configuration exported", extra={"source": "candidate"})
    return Configuration(provenance=_provenance("candidate", base_snapshot), sections=sections)


def of_cluster(base_snapshot: int | None, sections: Sections, admin: AdminValues) -> Configuration:
    """The Admin scope: the same Sections plus the Admin values that no Snapshot holds."""
    logger.info("configuration exported", extra={"source": "candidate", "admin": True})
    return Configuration(
        provenance=_provenance("candidate", base_snapshot), sections=sections, admin=admin
    )


class _Document(BaseModel):
    """What both scopes' imports share.

    Separate from `Configuration` rather than reused, because what is *required* differs in
    both directions. `provenance` is optional here -- it never gates an import, so demanding
    it would refuse a hand-written document for a field nothing reads -- and `sections` is
    keyed by plain strings so an unknown name can be reported as the Section it names
    instead of as a literal error reciting all six.

    The two scopes then differ in `admin` alone, which is the whole distinction between
    them, and they are siblings rather than parent and child because narrowing a field's
    type in a subclass is not something a type checker should have to accept.
    """

    model_config = ConfigDict(extra="forbid")

    format: Literal["apchi.configuration/v1"]
    provenance: Provenance | None = Field(
        default=None,
        description="Ignored. Kept in the model so an exported document imports unedited.",
    )
    sections: dict[str, dict[str, Any]] = Field(
        description=(
            "Every Section, each one whole. A replacement says what the configuration *is*, "
            "so an omitted Section is an error rather than an instruction to empty it."
        )
    )


class Import(_Document):
    """The Operator scope: the six Sections and nothing else.

    `admin` is typed as `None` rather than forbidden outright, and the difference matters:
    an Operator export serialises the field as null, so forbidding the key would make Apchi
    refuse its own output. A document carrying actual values is still rejected -- imported
    with them quietly dropped, an Operator would have no way to know values they cannot set
    had been lost.
    """

    admin: None = Field(
        default=None,
        description=(
            "Absent or null. An Operator export writes null here; an Admin document carries "
            "values, and this route must not apply values an Operator cannot set."
        ),
    )


class AdminImport(_Document):
    """The Admin scope: the same document, plus the Admin values.

    `admin` is required rather than defaulted. Defaulting it would mean an Operator
    document PUT here silently turned permission enforcement off and dropped an adopted
    Cluster's preserved rules -- a reset nobody asked for, arriving through an omission.
    """

    admin: AdminValues


def problems_with(sections: dict[str, dict[str, Any]]) -> list[ValidationFailure]:
    """Everything wrong with a document's Sections, all of it in one answer.

    Whole rather than first-wrong, because an Operator fixing a file one rejection at a
    time is what importing a whole configuration exists to avoid. And before anything is
    stored, because the Candidate is shared: one invalid resource in it would block
    everyone's Apply, not just the importer's (ADR-0002).

    Shape only. What one Section's resource implies about another is checked at Validate
    (`references`), and anything needing a real coordinator is checked against the probe.
    An import is an edit, so it gets exactly what an edit gets.
    """
    # Keyed by plain string, matching `sections`: a lookup by a name that may not be a
    # Section is the whole point of this function.
    known: dict[str, Section] = {section.name: section for section in REGISTERED}
    problems: list[ValidationFailure] = []

    for name in sorted(set(SECTIONS) - set(sections)):
        problems.append(
            ValidationFailure(
                section=name,
                reason=(
                    "is missing from the document. An import replaces the whole "
                    "configuration, so a Section that should be empty is written as {} "
                    "rather than left out."
                ),
            )
        )
    for key in sorted(sections):
        section = known.get(key)
        if section is None:
            problems.append(
                ValidationFailure(
                    reason=(
                        f"{key!r} is not a Section this Apchi manages. The Sections are: "
                        f"{', '.join(SECTIONS)}."
                    )
                )
            )
            continue
        problems.extend(section.read_back(sections[key]))
    return problems


def accepted(document: _Document) -> Sections:
    """The document's Sections, or `UnprocessablePayload` naming every problem at once."""
    if problems := problems_with(document.sections):
        raise UnprocessablePayload(
            f"This configuration has {len(problems)} problem(s), so none of it was stored.",
            details=[
                {
                    "section": problem.section,
                    "resource": problem.resource,
                    "problem": problem.reason,
                }
                for problem in problems
            ],
        )
    # Narrowed only now: `problems_with` has established every key is a Section name, which
    # is what the plain-string typing above bought.
    return cast(Sections, document.sections)

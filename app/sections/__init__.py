"""Sections: the managed areas of a Configuration Candidate.

A Section provides exactly four things to the pipeline -- a model, a configuration
generator, an apply strategy, and whether it requires a restart. That contract is
`app.sections.base`, and `app.sections.registry` is the list the pipeline walks, so
adding a Section is adding a module and registering it rather than changing the
pipeline.

This module holds only the vocabulary. The registry imports the Section implementations,
which is why it cannot live here: a Section that had to import the registry to learn its
own name would be a cycle.
"""

from typing import Literal, get_args

SectionName = Literal[
    "catalogs",
    "client_certificates",
    "certificate_mapping",
    "permissions",
    "resource_groups",
]

#: Sections that existed once and do not now. A Snapshot or Candidate written while one did
#: still holds it in MongoDB, and is read without it rather than refused: the Event Listener
#: became part of the Admin's Server Configuration, and an existing one is carried over into it
#: once, at startup (`app.pipeline.retired`).
#:
#: A type as well as a tuple, because records outlive the Section they name: an audit event or a
#: validation failure stored while it existed must still read.
RetiredSectionName = Literal["event_listeners"]
RETIRED_SECTIONS: tuple[str, ...] = get_args(RetiredSectionName)


def without_retired(sections: object) -> object:
    """A stored `sections` map with the retired Sections dropped, for a `before` validator."""
    if isinstance(sections, dict):
        return {name: value for name, value in sections.items() if name not in RETIRED_SECTIONS}
    return sections


# The registry lives in app.sections.registry, which imports the Section
# implementations -- so it cannot live here without a cycle. SECTIONS is derived there
# from what is actually registered.

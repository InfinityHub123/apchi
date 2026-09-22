"""Sections: the managed areas of a Configuration Candidate.

A Section provides exactly four things to the pipeline -- a model, a configuration
generator, an apply strategy, and whether it requires a restart. The pipeline knows
nothing else about it, so adding a Section is adding a module rather than changing
the pipeline.

Only Catalogs is registered in slice 1.
"""

from typing import Literal

SectionName = Literal[
    "catalogs",
    "client_certificates",
    "certificate_mapping",
    "permissions",
    "resource_groups",
    "event_listeners",
]

# Registered Sections. The rest of the vocabulary exists in SectionName so Review
# can report on every Section, but only these are editable.
SECTIONS: tuple[SectionName, ...] = ("catalogs",)

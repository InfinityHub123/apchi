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

from typing import Literal

SectionName = Literal[
    "catalogs",
    "client_certificates",
    "certificate_mapping",
    "permissions",
    "resource_groups",
    "event_listeners",
]

# The registry lives in app.sections.registry, which imports the Section
# implementations -- so it cannot live here without a cycle. SECTIONS is derived there
# from what is actually registered.

"""The DDL apply strategy for Catalogs.

Catalogs are the one Section applied by issuing SQL rather than writing a file, so
they need no restart and destroy no running queries. The cost is that Apply is not
atomic here: a sequence of statements can fail partway.

Creates and alters are ordered before drops, so a partial failure leaves a superset
of both states. An unused catalog breaks nobody; a missing one breaks every query
against it.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from app.adapters.trino import Trino

logger = logging.getLogger(__name__)


@dataclass
class CatalogPlan:
    """What Apply will do. Computed before anything is issued, so it can be logged
    and reported before the Cluster changes."""

    created: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.created or self.replaced or self.dropped)

    def summary(self) -> str:
        parts = []
        if self.created:
            parts.append(f"+{len(self.created)}")
        if self.replaced:
            parts.append(f"~{len(self.replaced)}")
        if self.dropped:
            parts.append(f"-{len(self.dropped)}")
        return " ".join(parts) or "no changes"


def plan(desired: dict[str, Any], current: dict[str, Any]) -> CatalogPlan:
    result = CatalogPlan()
    for name in sorted(desired):
        if name not in current:
            result.created.append(name)
        elif desired[name] != current[name]:
            result.replaced.append(name)
    result.dropped = sorted(set(current) - set(desired))
    return result


async def execute(trino: Trino, desired: dict[str, Any], plan_: CatalogPlan) -> None:
    """Issues the plan against the running coordinator.

    Creates and alters first, then drops. Trino has no ALTER CATALOG, so a replace
    is a drop followed by a create -- which means a replace briefly removes the
    catalog. That is unavoidable and is why a replace is reported separately.
    """
    for name in plan_.created:
        stored = desired[name]
        await trino.create_catalog(name, stored["connector"], stored.get("properties", {}))
        logger.info("created catalog", extra={"catalog": name})

    for name in plan_.replaced:
        stored = desired[name]
        await trino.drop_catalog(name)
        await trino.create_catalog(name, stored["connector"], stored.get("properties", {}))
        logger.info("replaced catalog", extra={"catalog": name})

    for name in plan_.dropped:
        await trino.drop_catalog(name)
        logger.info("dropped catalog", extra={"catalog": name})

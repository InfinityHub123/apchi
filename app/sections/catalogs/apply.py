"""The DDL apply strategy for Catalogs.

Catalogs are the one Section applied by issuing SQL rather than writing a file, so
they need no restart and destroy no running queries. The cost is that Apply is not
atomic here: a sequence of statements can fail partway.

Creates and alters are ordered before drops, so a partial failure leaves a superset
of both states. An unused catalog breaks nobody; a missing one breaks every query
against it.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.adapters.trino import Trino
from app.sections.base import ValidationFailure
from app.sections.catalogs import SECTION as CATALOGS

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


def rollback_plan(
    snapshot_catalogs: dict[str, Any],
    live: set[str],
    durable: dict[str, str],
    snapshot_copy: dict[str, str],
) -> CatalogPlan:
    """What it takes to put the Cluster's catalogs back to a Snapshot.

    A diff, and only a diff: the Snapshot says what should be there, `live` says what
    is, and the difference is what the failed Apply did. Nothing here consults what the
    Apply *intended*, so a partial DDL failure needs no guess about how far it got --
    and neither does a rollback attempted by an Apchi that has restarted since.

    Two records, both durable. The Snapshot is the desired state. `durable` is the
    catalog Secret as it stands, which is Apchi's record of what it manages: Apply
    writes it *before* issuing DDL (§10), so anything the DDL may have created is named
    in it. That is what separates a catalog this Apply added from one Apchi never put
    there -- Trino's own `system`, which cannot be dropped at all, or a catalog that
    predates Apchi and has not been through Adoption. Neither appears in the Secret, so
    neither is ever dropped.

    Trino will not report a catalog's properties back, so a *changed* catalog is
    invisible in `live`. It is found by comparing the two renderings instead: same name,
    different .properties content, so drop and recreate -- there is no ALTER CATALOG.
    """
    wanted = set(snapshot_catalogs)

    def rendered(name: str, source: dict[str, str]) -> str | None:
        return source.get(f"{name}.properties")

    return CatalogPlan(
        created=[name for name in sorted(wanted) if name not in live],
        replaced=[
            name
            for name in sorted(wanted)
            if name in live and rendered(name, durable) != rendered(name, snapshot_copy)
        ],
        dropped=[name for name in sorted(live - wanted) if rendered(name, durable) is not None],
    )


def collision_failures(created: Sequence[str], live: set[str]) -> list[ValidationFailure]:
    """A Catalog the Candidate would create must not already exist on the Cluster.

    This is the whole-Candidate check that has teeth in slice 1. It catches a name
    that collides with a catalog nobody brought under management -- one seeded before
    Apchi, say. The ephemeral probe cannot catch it, because the probe starts empty:
    the collision exists only on the Cluster.

    Without the check the DDL fails *after* Apply has written the Secret, which is
    the divergence of section 10 for a reason an Operator could have been told about
    before anything moved.
    """
    return [
        ValidationFailure(
            section=CATALOGS,
            resource=name,
            reason=(
                f"A catalog named {name!r} already exists on the Cluster and is not "
                "managed by Apchi. Adopt it or choose another name."
            ),
        )
        for name in sorted(set(created) & live)
    ]


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

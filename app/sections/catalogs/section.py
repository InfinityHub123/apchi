"""The Catalogs Section: the operations the pipeline and the API share.

These take the Section's own resources rather than the whole Configuration Candidate. A
Section has no business knowing about the aggregate it sits in -- and importing it would
invert the dependency, since the registry the pipeline reads imports this module.
"""

import logging
from typing import Any

from app.adapters.trino import Trino
from app.api.errors import NameAlreadyTaken, NotFound, UnprocessablePayload
from app.sections import SectionName
from app.sections.base import Cluster, Resources, SectionPlan, ValidationFailure
from app.sections.catalogs import SECTION, apply
from app.sections.catalogs.connectors import PropertyProblem, is_curated, validate_properties
from app.sections.catalogs.generator import render_secret
from app.sections.catalogs.model import Catalog, CatalogUpdate, CatalogWrite

logger = logging.getLogger(__name__)


def _as_catalog(name: str, stored: dict[str, Any]) -> Catalog:
    return Catalog(
        name=name,
        connector=stored["connector"],
        properties=stored.get("properties", {}),
        supported=is_curated(stored["connector"]),
    )


def list_catalogs(stored: Resources) -> list[Catalog]:
    return [_as_catalog(name, stored[name]) for name in sorted(stored)]


def get_catalog(stored: Resources, name: str) -> Catalog:
    if name not in stored:
        raise NotFound(f"No Catalog named {name!r} in the Configuration Candidate.")
    return _as_catalog(name, stored[name])


def _validated(connector: str, properties: dict[str, Any]) -> dict[str, str]:
    try:
        return validate_properties(connector, properties)
    except PropertyProblem as exc:
        raise UnprocessablePayload(
            f"The properties are not valid for the {connector!r} connector.",
            details=list(exc.problems),
        ) from exc


def create_catalog(stored: Resources, write: CatalogWrite) -> Catalog:
    if write.name in stored:
        raise NameAlreadyTaken(f"A Catalog named {write.name!r} already exists.")
    properties = _validated(write.connector, write.properties)
    stored[write.name] = {"connector": write.connector, "properties": properties}
    return _as_catalog(write.name, stored[write.name])


def update_catalog(stored: Resources, name: str, update: CatalogUpdate) -> Catalog:
    if name not in stored:
        raise NotFound(f"No Catalog named {name!r} in the Configuration Candidate.")
    current = stored[name]
    connector = update.connector or current["connector"]
    raw = current["properties"] if update.properties is None else update.properties
    stored[name] = {"connector": connector, "properties": _validated(connector, raw)}
    return _as_catalog(name, stored[name])


def delete_catalog(stored: Resources, name: str) -> None:
    if name not in stored:
        raise NotFound(f"No Catalog named {name!r} in the Configuration Candidate.")
    del stored[name]


class CatalogsSection:
    """The Catalogs Section as the pipeline sees it.

    Applied by DDL rather than by writing a file, which is why it needs no restart --
    and why it is the one Section whose Apply is not atomic. The ordering and
    compensation rules that follow from that live here now, rather than in the pipeline.
    """

    name: SectionName = SECTION
    #: Trino adopts a catalog through CREATE CATALOG against the running coordinator.
    requires_rollout = False

    def plan(self, desired: Resources, current: Resources) -> apply.CatalogPlan:
        return apply.plan(desired, current)

    async def apply(  # noqa: A003 - the pipeline's verb, not a shadowed builtin
        self, cluster: Cluster, desired: Resources, plan: SectionPlan
    ) -> None:
        """Write the Secret, then issue the DDL.

        This order is the whole point. A failed DDL leaves a catalog recorded but not
        live -- Apchi knows, because the DDL returned an error, and can retry. The
        reverse order leaves a catalog that works today and silently disappears at the
        next pod restart, possibly weeks later, with nothing connecting the two events.

        There is no propagation race: nothing reads the seed mount until the next pod
        start, so the Secret write needs no wait before the DDL.
        """
        assert isinstance(plan, apply.CatalogPlan)
        await cluster.kubernetes.write_secret(
            cluster.settings.catalog_secret_name, render_secret({SECTION: desired})
        )
        logger.info("patched the catalog Secret")
        await apply.execute(cluster.trino, desired, plan)

    async def restore(self, cluster: Cluster, snapshot: Resources) -> bool:
        """Rewrite the Secret and issue the compensating DDL.

        The plan is read before anything is written, because the Secret as the failed
        Apply left it is half of the diff that decides what to undo.
        """
        durable_copy = render_secret({SECTION: snapshot})
        durable_now = await cluster.kubernetes.read_secret(cluster.settings.catalog_secret_name)
        rollback = apply.rollback_plan(
            snapshot,
            await cluster.trino.catalogs(),
            durable_now,
            durable_copy,
        )
        logger.info("rollback planned", extra={"catalogs": rollback.summary()})

        await cluster.kubernetes.write_secret(cluster.settings.catalog_secret_name, durable_copy)
        logger.info("restored the catalog Secret")

        await apply.execute(cluster.trino, snapshot, rollback)
        # Catalogs never need a restart, so this answer costs nothing -- but it is the
        # honest one: something changed if the durable copy did or any DDL was issued.
        return durable_now != durable_copy or not rollback.empty

    async def check(
        self, cluster: Cluster, desired: Resources, plan: SectionPlan
    ) -> list[ValidationFailure]:
        """The collision the ephemeral coordinator cannot see, because it starts empty:
        a name already taken on the Cluster by a catalog nobody manages."""
        assert isinstance(plan, apply.CatalogPlan)
        if not plan.created:
            return []
        return apply.collision_failures(plan.created, await cluster.trino.catalogs())

    def needs_probe(self, desired: Resources) -> bool:
        return bool(desired)

    def probe_files(self, desired: Resources) -> dict[str, str]:
        """Nothing. Catalogs are proved by issuing statements against the probe, not by
        starting it with a file in place."""
        return {}

    async def check_against_probe(
        self, probe: Trino, desired: Resources
    ) -> list[ValidationFailure]:
        """Issue every CREATE CATALOG against the probe and collect what it rejects.

        Collecting is not swallowing: the operation still fails. An Operator fixing a
        Candidate wants the whole list, and each statement is independent of the others.
        """
        from trino.exceptions import TrinoQueryError

        failures: list[ValidationFailure] = []
        for name in sorted(desired):
            stored = desired[name]
            try:
                await probe.create_catalog(name, stored["connector"], stored.get("properties", {}))
            except TrinoQueryError as exc:
                failures.append(
                    ValidationFailure(section=SECTION, resource=name, reason=str(exc.message))
                )
            except ValueError as exc:
                # A connector name Apchi will not put in a statement at all.
                failures.append(ValidationFailure(section=SECTION, resource=name, reason=str(exc)))
        return failures

    async def verify(self, cluster: Cluster, desired: Resources) -> list[str]:
        """What distinguishes "the coordinator came back up" from "the coordinator came
        back up running the configuration we just applied"."""
        missing = sorted(set(desired) - await cluster.trino.catalogs())
        if not missing:
            return []
        return [
            f"The coordinator is not serving {', '.join(missing)}: "
            "the configuration was applied but is not live."
        ]

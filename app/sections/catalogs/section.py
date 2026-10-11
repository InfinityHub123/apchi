"""The Catalogs Section: the operations the pipeline and the API share.

These take the Section's own resources rather than the whole Configuration Candidate. A
Section has no business knowing about the aggregate it sits in -- and importing it would
invert the dependency, since the registry the pipeline reads imports this module.
"""

import logging
from collections.abc import Mapping
from typing import Any

from app.adapters.trino import Trino
from app.api.errors import NameAlreadyTaken, NotFound, UnprocessablePayload
from app.config import Settings
from app.sections import SectionName
from app.sections.admin import AdminValues
from app.sections.base import (
    Cluster,
    CoordinatorFile,
    DiscoveredPaths,
    Parsed,
    ParseProblem,
    Resources,
    SectionPlan,
    SmokeQuery,
    Unaccounted,
    ValidationFailure,
)
from app.sections.catalogs import SECTION, apply
from app.sections.catalogs.certificates import WIRED, ssl_is_configured
from app.sections.catalogs.connectors import PropertyProblem, is_curated, validate_properties
from app.sections.catalogs.generator import (
    Unreadable,
    effective,
    name_of,
    parse_properties,
    render_secret,
)
from app.sections.catalogs.model import Catalog, CatalogUpdate, CatalogWrite

logger = logging.getLogger(__name__)

#: Trino's own default for `catalog.config-dir`, relative to its working directory
#: /data/trino, whose `etc` symlinks to /etc/trino. Where an un-adopted Cluster keeping its
#: catalogs as a mounted ConfigMap usually has them.
_DEFAULT_STORE_DIR = "/etc/trino/catalog"


def _as_catalog(name: str, stored: dict[str, Any]) -> Catalog:
    return Catalog(
        name=name,
        connector=stored["connector"],
        properties=stored.get("properties", {}),
        certificate=stored.get("certificate"),
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


def _certificate_usable(connector: str, properties: dict[str, str], certificate: str) -> None:
    """Refuse a certificate Apchi cannot actually wire, rather than accepting it silently.

    A `certificate` accepted and not wired is the worst outcome available: a Catalog that
    connects, without the certificate the Operator asked it to present.
    """
    if connector not in WIRED:
        raise UnprocessablePayload(
            f"Apchi does not know how the {connector!r} connector names a client "
            f"certificate. Reference it by hand instead: ${{cert:{certificate}}} and "
            f"${{key:{certificate}}} expand to the paths, in whichever property "
            f"{connector!r} documents for them.",
            details=[{"property": "certificate", "problem": f"{connector!r} is not wired"}],
        )
    if not ssl_is_configured(connector, properties):
        raise UnprocessablePayload(
            "A client certificate does nothing until the connection is told to use TLS. "
            "Set sslmode on the connection URL -- Apchi will not choose between `require` "
            "and `verify-full` for you.",
            details=[{"property": "properties", "problem": "no sslmode on the connection URL"}],
        )


def validated(write: CatalogWrite) -> dict[str, Any]:
    """The stored form of a Catalog, with everything about it judged.

    Separate from `create_catalog` because Adoption needs the judging without the staging:
    an Operator supplying the properties of a Catalog that already exists on the Cluster is
    answering a question, not creating anything, so the name is not theirs to collide with
    (#90). Sharing this is what makes "validated the way staging is" true rather than
    approximately true.
    """
    properties = _validated(write.connector, write.properties)
    if write.certificate:
        _certificate_usable(write.connector, properties, write.certificate)
    return {
        "connector": write.connector,
        "properties": properties,
        **({"certificate": write.certificate} if write.certificate else {}),
    }


def create_catalog(stored: Resources, write: CatalogWrite) -> Catalog:
    if write.name in stored:
        raise NameAlreadyTaken(f"A Catalog named {write.name!r} already exists.")
    stored[write.name] = validated(write)
    return _as_catalog(write.name, stored[write.name])


def update_catalog(stored: Resources, name: str, update: CatalogUpdate) -> Catalog:
    if name not in stored:
        raise NotFound(f"No Catalog named {name!r} in the Configuration Candidate.")
    current = stored[name]
    connector = update.connector or current["connector"]
    raw = current["properties"] if update.properties is None else update.properties
    certificate = update.certificate or current.get("certificate")
    properties = _validated(connector, raw)
    if certificate:
        _certificate_usable(connector, properties, certificate)
    stored[name] = {
        "connector": connector,
        "properties": properties,
        **({"certificate": certificate} if certificate else {}),
    }
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

    def coordinator_files(self, settings: Settings) -> tuple[CoordinatorFile, ...]:
        """None. Catalogs reach Trino as statements, not as a file the coordinator reads --
        the Secret they do have is a seed the Admin's initContainer copies, not a mount
        Apchi owns."""
        return ()

    def render_files(
        self, desired: Resources, settings: Settings, admin: AdminValues
    ) -> dict[str, str]:
        return {}

    def discover_paths(self, settings: Settings, properties: Mapping[str, str]) -> DiscoveredPaths:
        """The store directory, wherever Trino is pointed at it.

        A directory rather than files, because the catalogs *are* its contents and only the
        volume knows what they are. `catalog.config-dir` is the authority; Trino's own
        default when `catalog.store=file` is `etc/catalog`, which is where an un-adopted
        Cluster keeping its catalogs as a mounted ConfigMap usually has them.

        Often unreadable, and that is expected rather than a failure: on a Cluster Apchi
        already configured this is an `emptyDir`, which has no content Apchi can read
        through the API. `system.metadata.catalogs` is what covers that case, and the
        reconciliation in the pipeline is what puts the two together.
        """
        directory = properties.get("catalog.config-dir") or _DEFAULT_STORE_DIR
        return DiscoveredPaths(
            directories={settings.catalog_store_dir: directory},
            why=(
                None
                if "catalog.config-dir" in properties
                else (
                    f"no catalog.config-dir property was found, so Trino's own default "
                    f"{_DEFAULT_STORE_DIR} was read rather than a path the Cluster named"
                )
            ),
        )

    def parse_files(self, files: Mapping[str, str], settings: Settings) -> Parsed:
        """Each `<name>.properties` in the store directory, back into a staged Catalog.

        A certificate reference does not survive, and cannot: `effective` wires a
        certificate into the connector's own properties and expands `${cert:name}` into a
        path before anything is written, so the file holds a path and no record of the name
        it came from. A Catalog adopted this way keeps working -- the path is right -- but
        Apchi will not know the certificate is in use until an Operator says so. Reported by
        the round-trip check rather than guessed at from the path.
        """
        resources: Resources = {}
        unaccounted: list[Unaccounted] = []
        for path in sorted(files):
            name = name_of(path, settings.catalog_store_dir)
            if name is None:
                unaccounted.append(
                    Unaccounted(path=path, what="a file in the catalog store Apchi does not own")
                )
                continue
            try:
                connector, properties, lost = parse_properties(path, files[path])
            except Unreadable as exc:
                raise ParseProblem(exc.path, exc.reason) from exc
            resources[name] = {"connector": connector, "properties": properties}
            unaccounted.extend(Unaccounted(path=path, what=what) for what in lost)
        return Parsed(resources=resources, unaccounted=tuple(unaccounted))

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
                # The same properties the Cluster will get, certificate paths and all. The
                # files are not in the probe and do not need to be: CREATE CATALOG does not
                # open a connection, so a path to a file that is not there is accepted here
                # exactly as it is on the Cluster -- verified against a real coordinator.
                await probe.create_catalog(name, stored["connector"], effective(stored))
            except TrinoQueryError as exc:
                failures.append(
                    ValidationFailure(section=SECTION, resource=name, reason=str(exc.message))
                )
            except ValueError as exc:
                # A connector name Apchi will not put in a statement at all.
                failures.append(ValidationFailure(section=SECTION, resource=name, reason=str(exc)))
        return failures

    async def verify(self, cluster: Cluster, desired: Resources, smoke: SmokeQuery) -> list[str]:
        """What distinguishes "the coordinator came back up" from "the coordinator came
        back up running the configuration we just applied"."""
        missing = sorted(set(desired) - await cluster.trino.catalogs())
        if not missing:
            return []
        return [
            f"The coordinator is not serving {', '.join(missing)}: "
            "the configuration was applied but is not live."
        ]

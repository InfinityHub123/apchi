"""Discovery: what a Cluster is configured with, read off the Cluster.

The first half of Adoption (§15). Apchi manages what Apchi configured, and a Cluster that
already runs has configuration Apchi knows nothing about -- which matters more than it
sounds, because the durable copy of every catalog is a Secret Apchi rewrites and the
coordinator reseeds from it at every start. A catalog Apchi does not hold survives the first
Apply and disappears at the next restart, weeks later, for an unrelated reason (§10).

Discovery is deliberately a **read**. It writes no Candidate, no Secret and no pod template,
so running it against production is safe and an Operator sees what Apchi understood before
anything happens to their Cluster. What becomes of it is later: #101 seeds Apchi's own
Secrets, the Admin cuts the deployment over, and only then does an Apply produce Snapshot 1.

Two things shape the implementation, both established against a running Trino 483:

**Trino cannot be asked.** `system.metadata.catalogs` gives a name and a connector and no
properties; `SHOW CREATE CATALOG` does not exist; the loaded access-control rules have no
introspection surface. So the files are the only source, and `coordinator` is how they are
read.

**The files are not where Apchi would put them.** On a Cluster Apchi did not configure they
are wherever the Admin put them, so each Section is asked where to look given the
coordinator's own configuration -- `security.config-file` says where the rules are, and so
on. Guessing Apchi's own layout would find nothing and report an empty Cluster, which is the
worst possible answer.
"""

import logging
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from app.adapters.kubernetes import KubernetesAdapter
from app.config import Settings
from app.pipeline import preconditions
from app.pipeline.coordinator import PodSpec, content_at, files_under, source_of
from app.sections import SectionName
from app.sections.base import ParseProblem, ParsesFiles, Resources, parses_files
from app.sections.catalogs import SECTION as CATALOGS
from app.sections.client_certificates.generator import MOUNT_DIR as CERTIFICATE_DIR
from app.sections.permissions.generator import PROPERTIES_PATH
from app.sections.registry import REGISTERED
from app.sections.resource_groups.generator import MANAGER_PATH

logger = logging.getLogger(__name__)

#: Trino's own configuration, which is where discovery finds everything else. Read in this
#: order and merged, so a later file can name a path an earlier one did not.
#:
#: `config.properties` carries the authenticators' `user-mapping.file`;
#: `access-control.properties` carries `security.config-file`;
#: `resource-groups.properties` carries `resource-groups.config-file`. Each is at a fixed
#: path because Trino looks for it there and nowhere else.
_CONFIGURATION = ("/etc/trino/config.properties", PROPERTIES_PATH, MANAGER_PATH)

ProblemKind = Literal[
    #: Apchi cannot read a file at all: not parseable, or not readable through the API.
    "unreadable",
    #: Something in a file Apchi's model has no vocabulary for. §15 decides what becomes
    #: of these; a Section only reports them.
    "unaccounted",
    #: What Apchi parsed would not regenerate the file it read, so the parse lost
    #: something -- and the first Apply would then delete it.
    "lossy",
    #: A Section Apchi cannot read back yet. Reported rather than reported as empty,
    #: because empty looks like a Cluster with nothing configured.
    "unreadable_section",
    #: A §16 precondition the deployment does not meet.
    "precondition",
    #: A §16 precondition that can only be met at the cutover, not before.
    "cutover",
    #: Trino is serving a catalog Apchi can name and cannot reconstruct. The Operator has
    #: to supply its properties; Apchi will not invent them.
    "incomplete",
    #: Configured but not loaded -- Apchi found a catalog Trino is not serving, which
    #: usually means the coordinator has not restarted since it was added.
    "not_loaded",
]


class TrinoReader(Protocol):
    """The one thing discovery asks Trino. A protocol rather than the adapter, because
    discovery reads and a type that can only read says so."""

    async def catalog_connectors(self) -> dict[str, str]: ...


class Problem(BaseModel):
    """One reason a discovery is not the whole truth."""

    kind: ProblemKind
    section: SectionName | None = None
    path: str | None = None
    detail: str

    def __str__(self) -> str:
        where = " ".join(part for part in (self.section, self.path) if part)
        return f"{where}: {self.detail}" if where else self.detail


class SectionDiscovery(BaseModel):
    """What one Section was able to read."""

    section: SectionName
    resources: Resources = Field(default_factory=dict)
    #: Where Apchi looked, so an Operator can tell a Section that has nothing configured
    #: from one Apchi looked for in the wrong place.
    looked_at: list[str] = Field(default_factory=list)
    #: Why Apchi looked there, when it guessed rather than read it from the Cluster.
    guessed_because: str | None = None
    readable: bool = Field(
        default=True, description="False when Apchi cannot parse this Section's files yet."
    )
    #: Configuration this Section read that belongs to the Admin rather than the Candidate:
    #: rules Apchi's models cannot express, kept working beneath the Operator's own rather
    #: than dropped or refused (§13.3, invariant 9). Not a problem, and reported separately
    #: so it is visible -- preserved rules nobody can see are worse than no adoption, because
    #: an Operator would read the grants and not understand the access people actually have.
    admin: dict[str, Any] = Field(default_factory=dict)
    problems: list[Problem] = Field(default_factory=list)


class Discovery(BaseModel):
    """Everything discovery found, and everything it could not account for."""

    coordinator: str
    sections: list[SectionDiscovery] = Field(default_factory=list)
    #: Problems that belong to the Cluster rather than to a Section: the preconditions.
    problems: list[Problem] = Field(default_factory=list)

    @property
    def every_problem(self) -> list[Problem]:
        return [*self.problems, *(p for section in self.sections for p in section.problems)]

    @property
    def admin_values(self) -> dict[str, Any]:
        """Every Admin value discovered, merged across Sections.

        What #101 writes and what the Admin API shows. Flat because `AdminValues` is one
        document read in one go, so an Apply holds the Admin side of the Cluster frozen for
        its whole run (§14).
        """
        merged: dict[str, Any] = {}
        for section in self.sections:
            merged.update(section.admin)
        return merged

    @property
    def complete(self) -> bool:
        """Whether everything the Cluster has is something Apchi can hold.

        A `cutover` problem does not count against it: it names something the deployment
        must change *after* adoption, not something wrong now (§82's amendment). Everything
        else blocks the seed and the adoption that follow it.
        """
        return not [p for p in self.every_problem if p.kind != "cutover"]


class DiscoveryFailed(Exception):
    """Discovery could not run at all, which is different from finding problems."""


async def _configuration(kubernetes: KubernetesAdapter, spec: PodSpec) -> dict[str, str]:
    """Trino's own configuration, merged, as far as Apchi can read it.

    An absent file is not a problem here: a Cluster with no resource groups has no
    `resource-groups.properties`, and a Section that finds no property of its own falls back
    to where Apchi would put its file.
    """
    merged: dict[str, str] = {}
    for path in _CONFIGURATION:
        content = await content_at(kubernetes, spec, path)
        if content is None:
            continue
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, separator, value = stripped.partition("=")
            if separator:
                merged[key.strip()] = value.strip()
    return merged


async def _precondition_problems(
    kubernetes: KubernetesAdapter, spec: PodSpec, settings: Settings
) -> list[Problem]:
    """Every §16 precondition failure, with the one the cutover owns separated out.

    §15 says Adoption asserts the preconditions and fails loudly. Four of the five can be
    met before adoption. The fifth cannot: the catalog-seed initContainer copies from a
    Secret Apchi has not written yet, so meeting it early means the next restart seeds an
    empty store and every catalog disappears. It is reported as a cutover requirement
    instead -- see §82's amendment.
    """
    try:
        await preconditions.check(kubernetes, spec, settings)
    except preconditions.PreconditionFailed as failed:
        return [
            Problem(
                kind="cutover" if settings.catalog_secret_name in problem else "precondition",
                detail=problem,
            )
            for problem in failed.problems
        ]
    return []


async def discover(
    kubernetes: KubernetesAdapter, settings: Settings, trino: TrinoReader | None = None
) -> Discovery:
    """Read the Cluster. Writes nothing.

    `trino` is optional and its absence is reported rather than hidden. It is only needed
    for one thing, and that thing cannot be got any other way: a catalog created by DDL
    after the pod started lives in the coordinator's writable store, which Apchi cannot
    read, so Trino is the only witness that it exists at all.
    """
    try:
        raw = await kubernetes.deployment_pod_spec(settings.coordinator_deployment_name)
    except Exception as exc:
        # An empty discovery of a Cluster Apchi cannot see looks exactly like a Cluster with
        # nothing configured, and an Operator would adopt it and lose everything.
        raise DiscoveryFailed(
            f"Apchi cannot read the coordinator Deployment "
            f"{settings.coordinator_deployment_name!r}: {exc}"
        ) from exc

    spec = PodSpec.model_validate(raw)
    properties = await _configuration(kubernetes, spec)
    logger.info(
        "read the coordinator's configuration",
        extra={"properties": len(properties), "volumes": len(spec.volumes)},
    )

    discovery = Discovery(
        coordinator=settings.coordinator_deployment_name,
        problems=await _precondition_problems(kubernetes, spec, settings),
    )

    for section in REGISTERED:
        if not parses_files(section):
            discovery.sections.append(
                SectionDiscovery(
                    section=section.name,
                    readable=False,
                    problems=[
                        Problem(
                            kind="unreadable_section",
                            section=section.name,
                            detail=(
                                "Apchi cannot read this Section's configuration back yet, so "
                                "nothing here was discovered. Reported rather than left "
                                "empty, because empty would look like a Cluster with none."
                            ),
                        )
                    ],
                )
            )
            continue
        found = await _discover_section(kubernetes, spec, settings, section, properties)
        if section.name == CATALOGS:
            found = await _reconcile_catalogs(found, kubernetes, settings, trino)
        discovery.sections.append(found)

    logger.info(
        "discovery complete",
        extra={"problems": len(discovery.every_problem), "complete": discovery.complete},
    )
    return discovery


async def _discover_section(
    kubernetes: KubernetesAdapter,
    spec: PodSpec,
    settings: Settings,
    section: ParsesFiles,
    properties: dict[str, str],
) -> SectionDiscovery:
    where = section.discover_paths(settings, properties)
    found: dict[str, str] = {}
    problems: list[Problem] = []

    # Keyed by where Apchi would keep the file, holding what is at the path the Cluster
    # actually has it at. The parser only knows the former; the Cluster only has the latter.
    for canonical, path in sorted(where.files.items()):
        source = source_of(spec, None, path)
        content = await content_at(kubernetes, spec, path)
        if content is not None:
            found[canonical] = content
        elif source is not None:
            # Something mounts the path and Apchi still cannot read it: an emptyDir, or a
            # key deeper inside a directory mount than a ConfigMap key can express.
            problems.append(
                Problem(
                    kind="unreadable",
                    section=section.name,
                    path=path,
                    detail=(
                        f"volume {source.volume.name!r} provides this path, but Apchi cannot "
                        "read its content -- it is not a Secret or ConfigMap key it can "
                        "resolve."
                    ),
                )
            )
        elif path in properties.values():
            # Trino is configured to read a file nothing mounts. The Cluster is already
            # broken; an Admin should hear it from Apchi rather than from a failed restart.
            problems.append(
                Problem(
                    kind="unreadable",
                    section=section.name,
                    path=path,
                    detail=(
                        "Trino is configured to read this file and nothing mounts it. The "
                        "coordinator will not come back from its next restart."
                    ),
                )
            )

    for canonical, directory in sorted(where.directories.items()):
        listed = await files_under(kubernetes, spec, directory)
        if listed is None:
            continue
        # Re-keyed the same way, so a parser matching on its own directory prefix still
        # recognises a Cluster that keeps the files somewhere else entirely.
        prefix, actual = canonical.rstrip("/"), directory.rstrip("/")
        found.update(
            {f"{prefix}{path.removeprefix(actual)}": content for path, content in listed.items()}
        )

    resources: Resources = {}
    admin: dict[str, Any] = {}
    if found:
        try:
            parsed = section.parse_files(found, settings)
        except ParseProblem as problem:
            problems.append(
                Problem(
                    kind="unreadable",
                    section=section.name,
                    path=problem.path,
                    detail=problem.reason,
                )
            )
        else:
            resources = parsed.resources
            admin = parsed.admin
            problems.extend(
                Problem(
                    kind="unaccounted",
                    section=section.name,
                    path=found_problem.path,
                    detail=found_problem.what,
                )
                for found_problem in parsed.unaccounted
            )
            problems.extend(_lossy(section, settings, resources, admin, found))

    return SectionDiscovery(
        section=section.name,
        resources=resources,
        admin=admin,
        looked_at=where.everywhere_looked,
        guessed_because=where.why,
        problems=problems,
    )


def _lossy(
    section: ParsesFiles,
    settings: Settings,
    resources: Resources,
    admin: dict[str, Any],
    read: dict[str, str],
) -> list[Problem]:
    """Whether Apchi's model survives a round trip through the file it would write.

    Not byte equality against what was read. That was the first version of this and it was
    noise: Apchi **adds** its own rules to every file it generates -- the reserved identity,
    the catch-alls, the kill_query procedure -- so a hand-written file never matches
    byte-for-byte and every adoption looked lossy. A check that fires on everything says
    nothing.

    The property that actually matters is a fixpoint. Parse the file, render from what was
    parsed, parse that: if the second parse differs from the first, the render dropped
    something the model was holding, and the first Apply would write that loss to the
    Cluster. If they agree, Apchi can carry this configuration without changing it.

    Content the model never held is a different thing and is already reported -- as
    `Unaccounted` for what Apchi cannot express, or as an Admin value for what it preserves.
    So the two checks are complementary rather than redundant.
    """
    from app.sections.admin import AdminValues

    # Regenerated with the Admin values this Section just discovered rather than with
    # defaults. A Section whose file holds configuration Apchi keeps as an Admin value would
    # otherwise drop it on the way out and then report itself lossy for doing so.
    try:
        values = AdminValues.model_validate(admin)
    except ValidationError:
        return [
            Problem(
                kind="lossy",
                section=section.name,
                detail=(
                    "what this Section read cannot be held as Admin values, so Apchi could "
                    "not carry it through an Apply."
                ),
            )
        ]

    regenerated = section.render_files(resources, settings, values)
    if not regenerated:
        # Catalogs reach Trino as DDL and own no coordinator file, so there is nothing to
        # read back. Their loss is checked against their durable form instead (§7.1).
        return []
    try:
        again = section.parse_files(regenerated, settings)
    except ParseProblem as problem:
        return [
            Problem(
                kind="lossy",
                section=section.name,
                path=problem.path,
                detail=(
                    f"Apchi cannot read back the file it would write for this Section "
                    f"({problem.reason}), so applying what was discovered would leave a "
                    "Cluster Apchi could not discover again."
                ),
            )
        ]
    if again.resources == resources and again.admin == admin:
        return []
    return [
        Problem(
            kind="lossy",
            section=section.name,
            path=sorted(read)[0] if read else None,
            detail=(
                "what Apchi parsed does not survive being written back and read again, so "
                "the configuration it holds is not the configuration it would apply."
            ),
        )
    ]


#: Never adopted. It cannot be dropped, has no properties, and §10 already excludes it from
#: rollback for the same reason -- a Snapshot holding `system` would describe a Cluster
#: Apchi could not restore.
_NEVER_ADOPTED = frozenset({"system"})


async def _reconcile_catalogs(
    found: SectionDiscovery,
    kubernetes: KubernetesAdapter,
    settings: Settings,
    trino: TrinoReader | None,
) -> SectionDiscovery:
    """Catalogs, from three sources, none of them complete.

    The one Section whose configuration has a source that is not a file, which is why this
    is here rather than in the Section: reconciling across sources is pipeline work, the way
    reconciling across Sections is (§6, `references.py`).

    | Found in | Reported as |
    |---|---|
    | a readable source, and Trino | complete |
    | Trino only | incomplete -- name and connector known, properties must be supplied |
    | a readable source only | configured but not loaded |

    **Nothing is ever invented.** A catalog Apchi can name and cannot reconstruct is reported
    as needing properties, not given plausible ones. An adopted catalog with a wrong
    `connection-url` validates, applies, and fails at query time against the wrong database;
    a refusal to guess is the feature.
    """
    resources = dict(found.resources)
    problems = list(found.problems)
    looked_at = list(found.looked_at)

    # The seed Secret, read by name rather than through a mount: the initContainer that
    # consumes it is not a container whose mounts `source_of` walks, and on a Cluster being
    # adopted there may be no initContainer yet at all.
    seed = await kubernetes.read_secret(settings.catalog_secret_name)
    if seed:
        looked_at.append(f"Secret {settings.catalog_secret_name}")
        from_seed = _catalogs_from_seed(settings, seed)
        for name, stored in from_seed.items():
            resources.setdefault(name, stored)

    if trino is None:
        problems.append(
            Problem(
                kind="unreadable",
                section=CATALOGS,
                detail=(
                    "Apchi could not ask Trino which catalogs are loaded, so a catalog that "
                    "exists only in the coordinator's store would not be noticed -- and would "
                    "be gone at the first restart after adoption."
                ),
            )
        )
        return found.model_copy(
            update={"resources": resources, "problems": problems, "looked_at": looked_at}
        )

    try:
        live = await trino.catalog_connectors()
    except Exception as exc:
        problems.append(
            Problem(
                kind="unreadable",
                section=CATALOGS,
                detail=(
                    f"Apchi could not ask Trino which catalogs are loaded ({exc}), so a "
                    "catalog existing only in the coordinator's store would not be noticed."
                ),
            )
        )
        return found.model_copy(
            update={"resources": resources, "problems": problems, "looked_at": looked_at}
        )

    looked_at.append("system.metadata.catalogs")
    for name, connector in sorted(live.items()):
        if name in _NEVER_ADOPTED or name in resources:
            continue
        problems.append(
            Problem(
                kind="incomplete",
                section=CATALOGS,
                path=name,
                detail=(
                    f"Trino is serving {name!r} using the {connector!r} connector, and its "
                    "properties exist only in the coordinator's writable store. Supply them: "
                    "Apchi will not invent a connection it could not read."
                ),
            )
        )

    for name in sorted(set(resources) - set(live)):
        problems.append(
            Problem(
                kind="not_loaded",
                section=CATALOGS,
                path=name,
                detail=(
                    f"{name!r} is configured and Trino is not serving it, which usually means "
                    "the coordinator has not restarted since it was added."
                ),
            )
        )

    problems.extend(_catalogs_using_a_certificate(resources))
    return found.model_copy(
        update={"resources": resources, "problems": problems, "looked_at": looked_at}
    )


def _catalogs_from_seed(settings: Settings, seed: dict[str, str]) -> Resources:
    """The seed Secret's keys, back into staged Catalogs.

    The same files as the store directory holds, under the same names, so the Section's own
    parser does the work -- re-keyed to the store directory it expects.
    """
    from app.sections.catalogs.section import CatalogsSection

    directory = settings.catalog_store_dir.rstrip("/")
    files = {f"{directory}/{key}": value for key, value in seed.items()}
    return CatalogsSection().parse_files(files, settings).resources


def _catalogs_using_a_certificate(resources: Resources) -> list[Problem]:
    """Catalogs whose properties point into the certificate directory.

    The loss here is semantic rather than textual, which is why the generic round-trip check
    misses it. `effective` wires a certificate into the connector's own properties and
    expands `${cert:name}` into a path *before* anything is written, so the file holds a path
    and no record of the name it came from. Re-rendering that path produces the same bytes --
    the file round-trips perfectly -- and Apchi still does not know the catalog uses a
    certificate it manages.

    The consequence is specific: removing that certificate would be allowed, because the
    check that refuses to remove one a Catalog still references works off the name. So this
    is reported, and an Operator naming the certificate is what repairs it.
    """
    problems: list[Problem] = []
    for name, stored in sorted(resources.items()):
        pointing = sorted(
            key
            for key, value in stored.get("properties", {}).items()
            if isinstance(value, str) and value.startswith(f"{CERTIFICATE_DIR.rstrip('/')}/")
        )
        if pointing:
            problems.append(
                Problem(
                    kind="unaccounted",
                    section=CATALOGS,
                    path=name,
                    detail=(
                        f"{', '.join(pointing)} points into the certificate directory, so this "
                        "catalog uses a Client Certificate. Apchi reads a path and cannot tell "
                        "which certificate it is -- name it, or removing that certificate will "
                        "not be refused."
                    ),
                )
            )
    return problems

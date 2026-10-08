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
from typing import Literal

from pydantic import BaseModel, Field

from app.adapters.kubernetes import KubernetesAdapter
from app.config import Settings
from app.pipeline import preconditions
from app.pipeline.coordinator import PodSpec, content_at, files_under, source_of
from app.sections import SectionName
from app.sections.base import ParseProblem, ParsesFiles, Resources, parses_files
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
]


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
    kubernetes: KubernetesAdapter, settings: Settings, trino_catalogs: set[str] | None = None
) -> Discovery:
    """Read the Cluster. Writes nothing.

    `trino_catalogs` is accepted and unused here: catalogs are reconciled from three sources
    and that is #88, which will need it. Taking it now keeps the signature from changing
    under the API that #90 builds on top.
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
        discovery.sections.append(
            await _discover_section(kubernetes, spec, settings, section, properties)
        )

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
            problems.extend(
                Problem(
                    kind="unaccounted",
                    section=section.name,
                    path=found_problem.path,
                    detail=found_problem.what,
                )
                for found_problem in parsed.unaccounted
            )
            problems.extend(_lossy(section, settings, resources, found))

    return SectionDiscovery(
        section=section.name,
        resources=resources,
        looked_at=where.everywhere_looked,
        guessed_because=where.why,
        problems=problems,
    )


def _lossy(
    section: ParsesFiles, settings: Settings, resources: Resources, read: dict[str, str]
) -> list[Problem]:
    """Whether regenerating from what was parsed reproduces what was read.

    The check that turns discovery from a best-effort import into something an Operator can
    run against production. A parse that silently drops a field looks fine here and deletes
    that field from the Cluster at the first Apply, because the Apply rewrites these files
    from what Apchi holds.

    Only paths Apchi would write itself are compared. A file at a path of the Admin's
    choosing regenerates at Apchi's path instead, which is a difference of location rather
    than of content -- and the cutover is what resolves it.
    """
    from app.sections.admin import AdminValues

    regenerated = section.render_files(resources, settings, AdminValues())
    problems: list[Problem] = []
    for path, original in sorted(read.items()):
        if path not in regenerated:
            continue
        if regenerated[path] != original:
            problems.append(
                Problem(
                    kind="lossy",
                    section=section.name,
                    path=path,
                    detail=(
                        "what Apchi parsed would not regenerate this file, so the parse lost "
                        "something. Applying it would write the regenerated version over "
                        "what is there."
                    ),
                )
            )
    return problems

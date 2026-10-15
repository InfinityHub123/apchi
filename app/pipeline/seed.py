"""The seed: Apchi writes its own Secrets, and nothing else happens.

Step 3 of five (§82's amendment). Apchi has read the Cluster (#87-#89) and an Admin has
completed the reading (#90); now Apchi puts what it holds into **its own** Secrets. The
Admin's originals are never touched, so the Cluster goes on serving exactly what it was
serving, and making Apchi's copies live is a deployment change the Admin makes deliberately.

That ordering is the whole of why adoption is reversible. Delete these Secrets and nothing has
happened; revert the deployment after the cutover and the originals are still there.

Two things this must not do, and both are easy to do by accident:

**It must not patch the coordinator's pod template.** Four Sections declare a
`CoordinatorFile`, so a normal Apply mounts and unmounts files and triggers a Rollout. A seed
that did that would cut over by accident, at a moment nobody chose, on a Cluster still serving
the Admin's configuration. So this writes Secret contents and nothing else -- a different
operation from an Apply, not an Apply with a flag.

**It must not write a Snapshot.** A Snapshot is a configuration applied to the real Cluster
and *verified* (invariant 2, §9). After a seed nothing has been applied and nothing is
verifiable, because the Cluster is still reading the Admin's files. Snapshot 1 comes from the
Apply that follows the cutover (#91), where verification means something.
"""

import logging

from pydantic import BaseModel, Field

from app.adapters.kubernetes import KubernetesAdapter
from app.config import Settings
from app.pipeline import preconditions
from app.pipeline.coordinator import PodSpec, source_of
from app.pipeline.discoveries import StoredDiscovery
from app.pipeline.discovery import trino_configuration
from app.pipeline.files import by_secret
from app.sections.admin import AdminValues
from app.sections.base import parses_files
from app.sections.catalogs import SECTION as CATALOGS
from app.sections.catalogs.generator import render_secret as render_catalog_seed
from app.sections.registry import REGISTERED

logger = logging.getLogger(__name__)


class SeedRefused(Exception):
    """The discovery is not in a state worth writing to the Cluster."""


class CutoverChange(BaseModel):
    """One thing the Admin's deployment still has to do for Apchi's copies to be in force."""

    secret: str | None = Field(default=None, description="The Secret to mount, if any.")
    path: str = Field(description="Where it belongs in the coordinator.")
    whole_volume: bool = Field(
        description=(
            "Whether it must be mounted as a whole volume rather than with subPath. A "
            "subPath mount is frozen at pod creation, so Apchi's later writes never arrive."
        )
    )
    why: str


class Seeded(BaseModel):
    """What the seed wrote, and what the cutover still needs."""

    secrets: list[str] = Field(description="The Secrets Apchi wrote, in the order written.")
    changed: list[str] = Field(
        description="The Secrets whose contents this seed actually altered. Empty on a "
        "second seed of an unchanged discovery."
    )
    cutover: list[CutoverChange] = Field(
        description="What the Admin's deployment must change before Apchi's copies are what "
        "Trino reads. Apchi does not do this -- it is their deployment."
    )
    untouched: list[str] = Field(
        description="Secrets and ConfigMaps Apchi read the configuration out of and did not "
        "write. The Cluster is still serving these."
    )


def _contents(stored: StoredDiscovery, settings: Settings) -> dict[str, dict[str, str]]:
    """Every Secret Apchi owns, with what it holds, from the completed discovery.

    Rendered through each Section's own generator, so what a seed writes is byte-identical to
    what an Apply would write for the same configuration. A seed that wrote something else
    would make the Apply after the cutover a change rather than a confirmation.
    """
    admin = AdminValues.model_validate(stored.read.admin_values)
    secrets: dict[str, dict[str, str]] = {}
    for section in REGISTERED:
        resources = stored.resolved(section.name)
        declared = section.coordinator_files(settings)
        if declared:
            rendered = section.render_files(resources, settings, admin)
            for secret, data in by_secret(declared, rendered).items():
                secrets.setdefault(secret, {}).update(data)
        if section.name == CATALOGS:
            # Catalogs reach Trino as DDL and own no coordinator file, so their durable form
            # is this Secret and nothing renders it for them (§7.1).
            secrets[settings.catalog_secret_name] = render_catalog_seed({CATALOGS: resources})
    return secrets


async def cutover_changes(kubernetes: KubernetesAdapter, settings: Settings) -> list[CutoverChange]:
    """What the Admin's deployment must change before Apchi's copies are what Trino reads.

    Two kinds, and the first version of this got the set wrong by asking a third thing.

    **The §16 preconditions**, which are already the list of what Apchi needs from a
    deployment -- the whole-volume mounts, the catalog seed initContainer, the refresh
    period. Expressed here as the change to make rather than as what is wrong.

    **Paths that point at the Admin's files rather than Apchi's.** Located the way discovery
    locates them -- by following Trino's own configuration (#87) -- but read from the
    coordinator as it stands now rather than from the stored reading, so that the answer
    changes when the Admin cuts over.

    What it must *not* ask is that every file Apchi declares be mounted. Apchi adds and
    removes those volumes itself during an Apply, and for Event Listeners and Resource Groups
    the **absence** of the mount is how "none configured" is expressed -- demanding them
    mounted made a correctly cut-over Cluster look as though nothing had been done.
    """
    changes: list[CutoverChange] = []
    spec = PodSpec.model_validate(
        await kubernetes.deployment_pod_spec(settings.coordinator_deployment_name)
    )
    try:
        await preconditions.check(kubernetes, spec, settings)
    except preconditions.PreconditionFailed as failed:
        for problem in failed.problems:
            seeding = settings.catalog_secret_name in problem
            changes.append(
                CutoverChange(
                    secret=settings.catalog_secret_name if seeding else None,
                    path=settings.catalog_store_dir if seeding else "the coordinator's deployment",
                    whole_volume=True,
                    why=(
                        # Said here because it is the one change whose *order* matters, and
                        # getting the order wrong is destructive rather than inconvenient.
                        f"{problem} This is the one change that must come after the seed: "
                        "adding it first copies an empty Secret over the catalog store and "
                        "loses every catalog."
                        if seeding
                        else problem
                    ),
                )
            )

    # Asked of the coordinator as it is now, not of the discovery. The stored reading
    # records where Trino read *before* the cutover, so a check against it can never clear:
    # the discovery being adopted is deliberately the pre-cutover one (#82), because
    # re-reading mid-migration finds a Cluster with no resource groups at all. Reading the
    # live configuration asks the only question that matters -- where does Trino read this
    # Section from right now -- and gets "the Admin's path" before and "Apchi's" after.
    properties = await trino_configuration(kubernetes, spec)
    for section in REGISTERED:
        declared = {spec_.path for spec_ in section.coordinator_files(settings)}
        if not declared or not parses_files(section):
            continue
        for path in section.discover_paths(settings, properties).everywhere_looked:
            if path not in declared and not _within(path, declared):
                changes.append(
                    CutoverChange(
                        path=path,
                        whole_volume=False,
                        why=(
                            f"Trino reads {section.name} from here, and Apchi writes "
                            f"{sorted(declared)[0]}. Until the deployment points at Apchi's "
                            "path, an Apply changes a file nothing reads"
                        ),
                    )
                )
    return changes


def _within(path: str, declared: set[str]) -> bool:
    """Whether a path is inside a directory Apchi declares, which is the same place."""
    return any(path.startswith(f"{candidate.rstrip('/')}/") for candidate in declared)


async def seed(
    kubernetes: KubernetesAdapter, settings: Settings, stored: StoredDiscovery
) -> Seeded:
    """Write Apchi's Secrets from a completed discovery. Nothing else.

    Raises `SeedRefused` for a discovery with anything outstanding, for the same reason #91
    refuses to adopt one: nothing half-understood reaches the Cluster.
    """
    if not stored.complete:
        raise SeedRefused(
            "This discovery still has "
            f"{len(stored.outstanding)} thing(s) Apchi could not account for. Supply or "
            "withdraw them before seeding: a Secret written from a half-read Cluster is a "
            "Cluster Apchi would misconfigure at the cutover."
        )

    contents = _contents(stored, settings)
    changed: list[str] = []
    for name in sorted(contents):
        before = await kubernetes.read_secret_if_present(name)
        if before is not None and before == contents[name]:
            # Seeding twice is harmless and says so. An Admin who is unsure whether they
            # seeded should be able to find out by doing it again.
            #
            # `is not None` is load-bearing: a Secret that does not exist reads as empty,
            # so an empty Section's Secret looked as though it were already correct and was
            # never created. The cutover then could not mount it and the coordinator stuck
            # in Init -- which is how the Secrets Apchi owns must exist even when a Section
            # has nothing in it.
            continue
        await kubernetes.write_secret(name, contents[name])
        changed.append(name)

    spec = PodSpec.model_validate(
        await kubernetes.deployment_pod_spec(settings.coordinator_deployment_name)
    )
    read_from = sorted(
        {
            source.volume.secret_name or source.volume.config_map_name or source.volume.name
            for section in stored.read.sections
            for path in section.looked_at
            if not path.startswith("Secret ")
            and path != "system.metadata.catalogs"
            and (source := source_of(spec, None, path)) is not None
        }
        - set(contents)
    )

    seeded = Seeded(
        secrets=sorted(contents),
        changed=changed,
        cutover=await cutover_changes(kubernetes, settings),
        untouched=read_from,
    )
    logger.info(
        "seeded Apchi's Secrets",
        extra={"written": len(changed), "cutover": len(seeded.cutover)},
    )
    return seeded


async def unseeded(
    kubernetes: KubernetesAdapter, settings: Settings, stored: StoredDiscovery
) -> list[str]:
    """Apchi's Secrets whose contents are not what this discovery says they should be.

    How Adoption knows the seed has actually run, and run against *this* reading. A cutover
    performed without a seed points the coordinator at empty Secrets, which Validation and
    Verification would eventually catch -- but catching it here means saying "seed first"
    instead of failing an Apply halfway and rolling a Cluster back.
    """
    contents = _contents(stored, settings)
    stale: list[str] = []
    for name in sorted(contents):
        before = await kubernetes.read_secret_if_present(name)
        if before is None or before != contents[name]:
            stale.append(name)
    return stale

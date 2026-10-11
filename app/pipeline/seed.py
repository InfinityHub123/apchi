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
from app.pipeline.coordinator import PodSpec, source_of
from app.pipeline.discoveries import StoredDiscovery
from app.pipeline.files import by_secret
from app.sections.admin import AdminValues
from app.sections.base import CoordinatorDirectory
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


def _cutover(spec: PodSpec, settings: Settings) -> list[CutoverChange]:
    """What the deployment still has to change, read off the pod template as it stands.

    The same gaps discovery reports as preconditions (#87), expressed as the change to make
    rather than as what is wrong. An Admin moving to `charts/trino` gets all of it at once;
    an Admin keeping their own chart needs the list.
    """
    changes: list[CutoverChange] = []
    for section in REGISTERED:
        for declared in section.coordinator_files(settings):
            path = declared.path
            whole = isinstance(declared, CoordinatorDirectory)
            source = source_of(spec, None, path)
            if source is not None and source.volume.secret_name == declared.secret:
                if whole and source.mount.sub_path is not None:
                    changes.append(
                        CutoverChange(
                            secret=declared.secret,
                            path=path,
                            whole_volume=True,
                            why=(
                                "mounted with subPath, which is frozen at pod creation -- "
                                "Apchi's writes would never reach the coordinator"
                            ),
                        )
                    )
                continue
            changes.append(
                CutoverChange(
                    secret=declared.secret,
                    path=path,
                    whole_volume=whole,
                    why=(
                        "nothing mounts Apchi's Secret here, so Trino is still reading the "
                        "configuration this Cluster had before Apchi"
                        if source is None
                        else f"the volume here is {source.volume.name!r}, not Apchi's Secret"
                    ),
                )
            )
    if not any(
        volume.secret_name == settings.catalog_secret_name
        for container in spec.init_containers
        for mount in container.volume_mounts
        for volume in spec.volumes
        if volume.name == mount.name
    ):
        changes.append(
            CutoverChange(
                secret=settings.catalog_secret_name,
                path=settings.catalog_store_dir,
                whole_volume=True,
                why=(
                    "no initContainer seeds the catalog store from Apchi's Secret, so the "
                    "catalogs Apchi now holds would not be loaded at the next start. This is "
                    "the one change that must come *after* the seed: adding it first would "
                    "seed an empty store and lose every catalog"
                ),
            )
        )
    return changes


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
        before = await kubernetes.read_secret(name)
        if before == contents[name]:
            # Seeding twice is harmless and says so. An Admin who is unsure whether they
            # seeded should be able to find out by doing it again.
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
        cutover=_cutover(spec, settings),
        untouched=read_from,
    )
    logger.info(
        "seeded Apchi's Secrets",
        extra={"written": len(changed), "cutover": len(seeded.cutover)},
    )
    return seeded

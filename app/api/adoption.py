"""Adoption's own surface: the discovery an Admin reads, completes and adopts.

Admin-authority, per §14. Adoption decides what a Cluster's configuration *is*, it runs
once, and it writes Admin values -- none of which is an Operator's to do.

A resource rather than an action, deliberately. Apchi cannot recover everything: a Catalog
created by DDL after the pod started has its properties only in the coordinator's writable
store, and Apchi will not invent a connection it could not read (#88). So the discovery is a
document to read, correct and complete, and nothing reaches the Cluster until the seed and
the cutover that follow it (#101, #91).
"""

import logging
from typing import Any

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import (
    AdminStoreDep,
    ApplyStoreDep,
    CandidateStoreDep,
    DiscoveryStoreDep,
    KubernetesDep,
    SettingsDep,
    SnapshotStoreDep,
    TrinoDep,
)
from app.api.errors import Conflict, NotFound
from app.pipeline.applies import ApplyRecord, ApplyRunner
from app.pipeline.discoveries import StoredDiscovery
from app.pipeline.discovery import DiscoveryFailed, Problem, discover
from app.pipeline.high_water import HighWaterMark
from app.pipeline.seed import Seeded, SeedRefused, cutover_changes, seed, unseeded
from app.sections import SectionName
from app.sections.admin import AdminValues
from app.sections.catalogs import SECTION as CATALOGS
from app.sections.catalogs.model import CatalogWrite
from app.sections.catalogs.section import validated as validated_catalog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/adoption", tags=["adoption"])


class DiscoveryView(BaseModel):
    """A discovery as an Admin reads it."""

    model_config = ConfigDict(extra="forbid")

    read_at: str
    complete: bool = Field(
        description="Whether everything Apchi could not read has been supplied. "
        "Seeding and adopting both require it."
    )
    outstanding: list[Problem] = Field(
        description="Problems no amendment has answered yet, each naming where it is."
    )
    changed_since_last_read: list[str] = Field(
        description="What the Cluster changed between the previous reading and this one."
    )
    sections: list[dict[str, Any]] = Field(
        description="Per Section: what was read, where Apchi looked, and what it amended."
    )
    admin_values: dict[str, Any] = Field(
        description="Configuration that belongs to the Admin rather than the Candidate: "
        "rules Apchi's models cannot express, kept working beneath the Operator's own."
    )


def _view(stored: StoredDiscovery) -> DiscoveryView:
    return DiscoveryView(
        read_at=stored.read_at.isoformat(),
        complete=stored.complete,
        outstanding=stored.outstanding,
        changed_since_last_read=stored.changed_since_last_read,
        sections=[
            {
                "section": section.section,
                "readable": section.readable,
                "looked_at": section.looked_at,
                "guessed_because": section.guessed_because,
                "resources": stored.resolved(section.section),
                "amended": sorted(stored.amendments.get(section.section, {})),
                "problems": [problem.model_dump(mode="json") for problem in section.problems],
            }
            for section in stored.read.sections
        ],
        admin_values=stored.read.admin_values,
    )


@router.post(
    "/discoveries",
    response_model=DiscoveryView,
    status_code=status.HTTP_201_CREATED,
    summary="Read the Cluster's configuration, changing nothing",
)
async def create_discovery(
    store: DiscoveryStoreDep,
    kubernetes: KubernetesDep,
    trino: TrinoDep,
    settings: SettingsDep,
) -> DiscoveryView:
    """Reads and records. Touches no part of the Cluster and not the Candidate.

    Re-reading keeps whatever has been amended and reports what the Cluster changed, because
    an Admin re-reads precisely when they are unsure -- the worst moment to take their work
    away.
    """
    try:
        read = await discover(kubernetes, settings, trino)
    except DiscoveryFailed as failed:
        raise Conflict(str(failed)) from failed
    return _view(await store.record(read))


@router.get("/discovery", response_model=DiscoveryView, summary="The current discovery")
async def get_discovery(store: DiscoveryStoreDep) -> DiscoveryView:
    stored = await store.load()
    if stored is None:
        raise NotFound("No discovery has been read. POST /admin/adoption/discoveries first.")
    return _view(stored)


@router.delete(
    "/discovery",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Discard the discovery and everything supplied for it",
)
async def discard_discovery(store: DiscoveryStoreDep) -> None:
    """Discards Apchi's reading and the amendments with it. Nothing on the Cluster is
    touched -- a discovery never reached it."""
    await store.discard()


@router.post(
    "/seed",
    response_model=Seeded,
    summary="Write Apchi's own Secrets from a completed discovery",
)
async def seed_cluster(
    store: DiscoveryStoreDep,
    kubernetes: KubernetesDep,
    settings: SettingsDep,
) -> Seeded:
    """Writes Apchi's Secrets and nothing else: no mount, no Rollout, no Snapshot, and not
    one byte of the Secrets Apchi read the configuration out of.

    So the Cluster goes on serving exactly what it was serving. Making these copies live is
    the next step and it is the Admin's: re-point the deployment, add the catalog-seed
    initContainer, set a refresh period. The response says precisely what is left.

    Reversible at this point by deleting the Secrets -- nothing has happened yet.
    """
    stored = await store.load()
    if stored is None:
        raise NotFound("No discovery has been read. POST /admin/adoption/discoveries first.")
    try:
        return await seed(kubernetes, settings, stored)
    except SeedRefused as refused:
        raise Conflict(str(refused)) from refused


@router.post(
    "/adopt",
    response_model=ApplyRecord,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Adopt the Cluster, producing Snapshot 1",
)
async def adopt(
    request: Request,
    store: DiscoveryStoreDep,
    candidate_store: CandidateStoreDep,
    snapshots: SnapshotStoreDep,
    applies: ApplyStoreDep,
    admin_store: AdminStoreDep,
    kubernetes: KubernetesDep,
    settings: SettingsDep,
    discard_history_through: int | None = Query(
        default=None,
        description=(
            "Only for a Cluster that has committed Snapshots MongoDB no longer holds. "
            "Repeat the Snapshot number Apchi says it once reached, to confirm that "
            "adopting may start again at 1 without those Snapshots. A number rather than a "
            "flag, so it cannot be set out of habit."
        ),
    ),
) -> ApplyRecord:
    """Stages the completed discovery and then gets out of the way.

    Deliberately the smallest thing in Adoption. §15 is explicit that this is not a special
    case of Commit: the discovered configuration is validated, applied to the real Cluster
    and verified, and the Snapshot it produces is a Snapshot like any other (invariant 2).
    So `/validations` and `/applies` do the rest, unchanged, and there is no second route to
    the Cluster.

    Which leaves the refusals as the substance, and all four protect something:
    """
    if await snapshots.latest() is not None:
        raise Conflict(
            "This Cluster already has Snapshots, so it is already managed. Adoption produces "
            "Snapshot 1 and runs once; editing the Candidate is how a managed Cluster "
            "changes."
        )

    # The check above asks MongoDB, and an empty `snapshots` collection is two situations:
    # a Cluster that was never managed, and one that is managed by an Apchi whose database
    # is empty right now. The first should be adopted and the second must not be -- adopting
    # it mints Snapshot 1 over a Cluster that already reached thirteen. The mark is the only
    # thing that tells them apart.
    ever = await HighWaterMark(kubernetes, settings).read()
    if ever is not None and discard_history_through != ever:
        raise Conflict(
            f"This Cluster has committed Snapshot {ever}, so it is already managed, and "
            "MongoDB holds none of those Snapshots. Adopting now produces a new Snapshot 1 "
            "and does not bring them back. Point Apchi at the database that holds them, or "
            "-- if they are genuinely gone and you want to start again from what the "
            "Cluster is running -- repeat the number: POST "
            f"/api/v1/admin/adoption/adopt?discard_history_through={ever}"
        )
    if (running := await applies.in_flight()) is not None:
        raise Conflict(f"Apply {running.id} is already in flight.")

    stored = await store.load()
    if stored is None:
        raise NotFound("No discovery has been read. POST /admin/adoption/discoveries first.")
    if not stored.complete:
        raise Conflict(
            f"This discovery still has {len(stored.outstanding)} thing(s) Apchi could not "
            "account for. Nothing half-understood should reach the Cluster."
        )

    if outstanding := await cutover_changes(kubernetes, settings):
        raise Conflict(
            "The cutover has not happened: "
            + "; ".join(f"{change.path} -- {change.why}" for change in outstanding)
            + ". Until the coordinator reads Apchi's Secrets there is nothing for "
            "Verification to verify, and a Snapshot claiming otherwise would be the "
            "never-verified Snapshot §15 refuses to create."
        )
    if stale := await unseeded(kubernetes, settings, stored):
        raise Conflict(
            f"Apchi's Secrets do not hold this discovery yet ({', '.join(stale)}). "
            "POST /admin/adoption/seed first -- the coordinator is reading Secrets that do "
            "not say what was discovered."
        )

    # Admin values first. They are applied and never recorded in a Snapshot (invariant 9), so
    # the Apply has to be able to read them -- and the preserved access-control rules are the
    # reason a Cluster keeps the access it had on the day it was onboarded.
    await admin_store.save(AdminValues.model_validate(stored.read.admin_values))

    candidate = await candidate_store.load()
    for section in stored.read.sections:
        candidate.sections[section.section] = stored.resolved(section.section)
    await candidate_store.save(candidate)

    record = await applies.create(base_snapshot=candidate.base_snapshot)
    runner: ApplyRunner = request.app.state.apply_runner
    runner.start(record)
    logger.info("adoption started", extra={"apply": record.id})
    return record


@router.put(
    "/discovery/catalogs/{name}",
    response_model=DiscoveryView,
    summary="Supply the properties of a Catalog Apchi could not read",
)
async def amend_catalog(name: str, write: CatalogWrite, store: DiscoveryStoreDep) -> DiscoveryView:
    """Judged exactly as `POST /catalogs` judges a staged Catalog, through the same code.

    The name in the path wins: an amendment answers a question Apchi asked about a named
    Catalog, so a body naming a different one is a mistake worth refusing rather than
    quietly resolving.
    """
    if write.name != name:
        raise Conflict(
            f"The body names {write.name!r} and the path names {name!r}. An amendment "
            "answers Apchi's question about one Catalog."
        )
    stored = await store.load()
    if stored is None:
        raise NotFound("No discovery has been read. POST /admin/adoption/discoveries first.")
    return _view(await store.amend(CATALOGS, name, validated_catalog(write)))


@router.delete(
    "/discovery/{section}/{resource}",
    response_model=DiscoveryView,
    summary="Withdraw something supplied for a discovery",
)
async def withdraw_amendment(
    section: SectionName, resource: str, store: DiscoveryStoreDep
) -> DiscoveryView:
    try:
        return _view(await store.withdraw(section, resource))
    except LookupError as exc:
        raise NotFound(
            "No discovery has been read. POST /admin/adoption/discoveries first."
        ) from exc
    except KeyError as exc:
        raise NotFound(f"Nothing was supplied for {resource!r} in {section!r}.") from exc

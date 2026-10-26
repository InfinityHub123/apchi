"""Catalogs. Every change lands in the Configuration Candidate and reaches nothing
else -- not Trino, not Kubernetes. Apply is what makes a change real."""

from fastapi import APIRouter, status

from app.api.deps import (
    CandidateStoreDep,
    ClusterDep,
    OperatorMutationAllowed,
    SnapshotStoreDep,
    TrinoDep,
)
from app.pipeline.recovery import RevertEffect, RevertRequest, section_revert
from app.sections.catalogs import SECTION as CATALOGS
from app.sections.catalogs import section
from app.sections.catalogs.model import Catalog, CatalogUpdate, CatalogWrite

router = APIRouter(prefix="/catalogs", tags=["catalogs"])


@router.post(
    "/revert",
    response_model=RevertEffect,
    summary="Stage this Section's content from an earlier Snapshot",
    dependencies=[OperatorMutationAllowed],
)
async def revert(
    request: RevertRequest,
    store: CandidateStoreDep,
    snapshots: SnapshotStoreDep,
    cluster: ClusterDep,
) -> RevertEffect:
    """Stages into the Candidate; it does not apply. An ordinary POST /applies follows,
    like any other edit.

    The response says which catalogs an Apply would drop, and that reverting one Section
    while the others stay put produces a configuration that has never run.
    """
    return await section_revert(store, snapshots, cluster, CATALOGS, request.snapshot)


@router.get("", response_model=list[Catalog], summary="List staged Catalogs")
async def list_catalogs(store: CandidateStoreDep) -> list[Catalog]:
    return section.list_catalogs((await store.load()).resources(CATALOGS))


@router.post(
    "",
    response_model=Catalog,
    status_code=status.HTTP_201_CREATED,
    summary="Stage a new Catalog",
    dependencies=[OperatorMutationAllowed],
)
async def create_catalog(write: CatalogWrite, store: CandidateStoreDep, trino: TrinoDep) -> Catalog:
    await section.refuse_unknown_connector(trino, write.connector)
    candidate = await store.load()
    created = section.create_catalog(candidate.resources(CATALOGS), write)
    await store.save(candidate)
    return created


@router.get("/{name}", response_model=Catalog, summary="Fetch a staged Catalog")
async def get_catalog(name: str, store: CandidateStoreDep) -> Catalog:
    return section.get_catalog((await store.load()).resources(CATALOGS), name)


@router.patch(
    "/{name}",
    response_model=Catalog,
    summary="Edit a staged Catalog",
    dependencies=[OperatorMutationAllowed],
)
async def update_catalog(
    name: str, update: CatalogUpdate, store: CandidateStoreDep, trino: TrinoDep
) -> Catalog:
    # Only when the request actually sends one. An omitted connector keeps whatever is
    # staged, and re-checking that would refuse an edit to the properties of a Catalog whose
    # connector came from a Trino image the Cluster no longer runs.
    if update.connector is not None:
        await section.refuse_unknown_connector(trino, update.connector)
    candidate = await store.load()
    updated = section.update_catalog(candidate.resources(CATALOGS), name, update)
    await store.save(candidate)
    return updated


@router.delete(
    "/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a staged Catalog",
    dependencies=[OperatorMutationAllowed],
)
async def delete_catalog(name: str, store: CandidateStoreDep) -> None:
    candidate = await store.load()
    section.delete_catalog(candidate.resources(CATALOGS), name)
    await store.save(candidate)

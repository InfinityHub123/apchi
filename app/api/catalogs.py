"""Catalogs. Every change lands in the Configuration Candidate and reaches nothing
else -- not Trino, not Kubernetes. Apply is what makes a change real."""

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict

from app.api.deps import CandidateStoreDep, OperatorMutationAllowed, SnapshotStoreDep
from app.pipeline.recovery import RevertEffect, section_revert
from app.sections.catalogs import section
from app.sections.catalogs.model import Catalog, CatalogUpdate, CatalogWrite

router = APIRouter(prefix="/catalogs", tags=["catalogs"])


class RevertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot: int


@router.post(
    "/revert",
    response_model=RevertEffect,
    summary="Stage this Section's content from an earlier Snapshot",
    dependencies=[OperatorMutationAllowed],
)
async def revert(
    request: RevertRequest, store: CandidateStoreDep, snapshots: SnapshotStoreDep
) -> RevertEffect:
    """Stages into the Candidate; it does not apply. An ordinary POST /applies follows,
    like any other edit.

    The response says which catalogs an Apply would drop, and that reverting one Section
    while the others stay put produces a configuration that has never run.
    """
    return await section_revert(store, snapshots, section.SECTION, request.snapshot)


@router.get("", response_model=list[Catalog], summary="List staged Catalogs")
async def list_catalogs(store: CandidateStoreDep) -> list[Catalog]:
    return section.list_catalogs(await store.load())


@router.post(
    "",
    response_model=Catalog,
    status_code=status.HTTP_201_CREATED,
    summary="Stage a new Catalog",
    dependencies=[OperatorMutationAllowed],
)
async def create_catalog(write: CatalogWrite, store: CandidateStoreDep) -> Catalog:
    candidate = await store.load()
    created = section.create_catalog(candidate, write)
    await store.save(candidate)
    return created


@router.get("/{name}", response_model=Catalog, summary="Fetch a staged Catalog")
async def get_catalog(name: str, store: CandidateStoreDep) -> Catalog:
    return section.get_catalog(await store.load(), name)


@router.patch(
    "/{name}",
    response_model=Catalog,
    summary="Edit a staged Catalog",
    dependencies=[OperatorMutationAllowed],
)
async def update_catalog(name: str, update: CatalogUpdate, store: CandidateStoreDep) -> Catalog:
    candidate = await store.load()
    updated = section.update_catalog(candidate, name, update)
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
    section.delete_catalog(candidate, name)
    await store.save(candidate)

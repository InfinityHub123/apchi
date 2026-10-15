"""Permissions: who may reach what.

An Operator grants a Trino Identity privileges on a catalog, a schema or a table, and never
sees Trino's authorization JSON. Apchi generates the whole file from these grants and the
rules it owns itself.

A grant is addressed by a key Apchi derives from the identity and the place, so the same
grant is always the same resource and Review can name the one that changed.
"""

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import (
    AdminStoreDep,
    CandidateStoreDep,
    ClusterDep,
    OperatorMutationAllowed,
    SnapshotStoreDep,
)
from app.api.errors import Conflict
from app.pipeline.recovery import RevertEffect, RevertRequest, section_revert
from app.sections.permissions import SECTION as PERMISSIONS
from app.sections.permissions import section
from app.sections.permissions.model import Grant, GrantWrite, Privilege, SystemRules

router = APIRouter(prefix="/permissions", tags=["permissions"])


class PrivilegesWrite(BaseModel):
    """What a staged grant can be changed to."""

    model_config = ConfigDict(extra="forbid")

    privileges: list[Privilege] = Field(min_length=1)


# Declared before `/{key}`: a key is one path segment, so a route matching it would shadow
# these.
@router.get(
    "/system",
    response_model=SystemRules,
    summary="The rules Apchi owns and nobody may edit",
)
async def get_system_rules(admin: AdminStoreDep) -> SystemRules:
    """Visible on purpose. An Operator who cannot see these cannot understand why catalog
    DDL is refused to them -- or, depending on the posture an Admin has set, why a grant
    does not narrow anyone's access yet."""
    return section.system_rules(await admin.load())


@router.put("/system", summary="Refused: these rules are Apchi's", include_in_schema=False)
async def set_system_rules() -> None:
    raise Conflict(
        "The system-owned rules are generated, not configured. They exist to keep catalog "
        "DDL restricted to Apchi and to keep Apchi able to reach the Cluster it manages; "
        "an Operator who could edit them could lock Apchi out of its own recovery."
    )


@router.get("", response_model=list[Grant], summary="The staged grants")
async def list_grants(store: CandidateStoreDep) -> list[Grant]:
    return section.list_grants((await store.load()).resources(PERMISSIONS))


@router.post(
    "",
    response_model=Grant,
    status_code=status.HTTP_201_CREATED,
    summary="Grant an identity privileges",
    dependencies=[OperatorMutationAllowed],
)
async def add_grant(write: GrantWrite, store: CandidateStoreDep) -> Grant:
    candidate = await store.load()
    saved = section.add_grant(candidate.resources(PERMISSIONS), write)
    await store.save(candidate)
    return saved


@router.get("/{key}", response_model=Grant, summary="One grant")
async def get_grant(key: str, store: CandidateStoreDep) -> Grant:
    return section.get_grant((await store.load()).resources(PERMISSIONS), key)


@router.put(
    "/{key}",
    response_model=Grant,
    summary="Change what a grant allows",
    dependencies=[OperatorMutationAllowed],
)
async def set_privileges(key: str, write: PrivilegesWrite, store: CandidateStoreDep) -> Grant:
    """The privileges are what changes. Where a grant applies is its identity -- moving it
    is removing one grant and adding another, which is what an Operator means anyway."""
    candidate = await store.load()
    saved = section.set_privileges(candidate.resources(PERMISSIONS), key, write.privileges)
    await store.save(candidate)
    return saved


@router.delete(
    "/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a grant",
    dependencies=[OperatorMutationAllowed],
)
async def delete_grant(key: str, store: CandidateStoreDep) -> None:
    candidate = await store.load()
    section.delete_grant(candidate.resources(PERMISSIONS), key)
    await store.save(candidate)


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
    return await section_revert(store, snapshots, cluster, PERMISSIONS, request.snapshot)

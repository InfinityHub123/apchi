"""Resource Groups: the hierarchy, and the selectors that choose within it.

Groups are addressed by their dotted path, because that is what they are: `global.etl` is
the group `etl` under `global`. The selectors are read and replaced as one list, because
first match wins and editing one in place would let an Operator change what matches without
seeing what now shadows it.
"""

from fastapi import APIRouter, status
from pydantic import ConfigDict

from app.api.deps import (
    CandidateStoreDep,
    ClusterDep,
    OperatorMutationAllowed,
    SnapshotStoreDep,
)
from app.pipeline.recovery import RevertEffect, RevertRequest, section_revert
from app.sections.resource_groups import SECTION as GROUPS
from app.sections.resource_groups import section
from app.sections.resource_groups.model import (
    GroupPath,
    ResourceGroup,
    ResourceGroupSettings,
    ResourceGroupWrite,
    Selectors,
)

router = APIRouter(prefix="/resource-groups", tags=["resource groups"])


class ResourceGroupCreate(ResourceGroupWrite):
    """A group and where it belongs. The parent must already be staged."""

    #: A child of `global`, which has to be staged already. Memory as a percentage
    #: rather than a byte count, because a cluster that is resized should not need
    #: every group rewritten.
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "path": "global.etl",
                    "hard_concurrency_limit": 10,
                    "soft_memory_limit": "40%",
                    "max_queued": 100,
                    "scheduling_policy": "fair",
                }
            ]
        }
    )

    path: GroupPath


# Declared before the `{path}` routes: `selectors` is a legal path segment, and a route
# matching it as one would shadow this.
@router.get("/selectors", response_model=Selectors, summary="The staged selectors, in order")
async def get_selectors(store: CandidateStoreDep) -> Selectors:
    return section.get_selectors((await store.load()).resources(GROUPS))


@router.put(
    "/selectors",
    response_model=Selectors,
    summary="Replace the selectors",
    dependencies=[OperatorMutationAllowed],
)
async def set_selectors(selectors: Selectors, store: CandidateStoreDep) -> Selectors:
    """Whole-list replacement is the contract. A selector naming a group that is not staged
    yet is accepted here and refused at Validate, so an Operator can write the rule before
    the group it points at."""
    candidate = await store.load()
    saved = section.set_selectors(candidate.resources(GROUPS), selectors)
    await store.save(candidate)
    return saved


@router.get("/settings", response_model=ResourceGroupSettings, summary="The Section's own settings")
async def get_settings(store: CandidateStoreDep) -> ResourceGroupSettings:
    return section.get_settings((await store.load()).resources(GROUPS))


@router.put(
    "/settings",
    response_model=ResourceGroupSettings,
    summary="Replace the Section's own settings",
    dependencies=[OperatorMutationAllowed],
)
async def set_settings(
    settings: ResourceGroupSettings, store: CandidateStoreDep
) -> ResourceGroupSettings:
    """The CPU quota period governs the whole file rather than any one group, and Trino
    refuses to start when a group sets a CPU limit without it."""
    candidate = await store.load()
    saved = section.set_settings(candidate.resources(GROUPS), settings)
    await store.save(candidate)
    return saved


@router.get("", response_model=list[ResourceGroup], summary="The staged resource groups")
async def list_resource_groups(store: CandidateStoreDep) -> list[ResourceGroup]:
    return section.list_groups((await store.load()).resources(GROUPS))


@router.post(
    "",
    response_model=ResourceGroup,
    status_code=status.HTTP_201_CREATED,
    summary="Add a resource group",
    dependencies=[OperatorMutationAllowed],
)
async def add_resource_group(write: ResourceGroupCreate, store: CandidateStoreDep) -> ResourceGroup:
    candidate = await store.load()
    body = ResourceGroupWrite.model_validate(write.model_dump(exclude={"path"}))
    saved = section.add_group(candidate.resources(GROUPS), write.path, body)
    await store.save(candidate)
    return saved


@router.get("/{path}", response_model=ResourceGroup, summary="One resource group")
async def get_resource_group(path: str, store: CandidateStoreDep) -> ResourceGroup:
    return section.get_group((await store.load()).resources(GROUPS), path)


@router.put(
    "/{path}",
    response_model=ResourceGroup,
    summary="Replace a resource group's configuration",
    dependencies=[OperatorMutationAllowed],
)
async def edit_resource_group(
    path: str, write: ResourceGroupWrite, store: CandidateStoreDep
) -> ResourceGroup:
    candidate = await store.load()
    saved = section.edit_group(candidate.resources(GROUPS), path, write)
    await store.save(candidate)
    return saved


@router.delete(
    "/{path}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a resource group",
    dependencies=[OperatorMutationAllowed],
)
async def delete_resource_group(path: str, store: CandidateStoreDep) -> None:
    """Refused while it still has subgroups: deleting a parent would take its whole subtree
    with it, and an Operator should do that deliberately.

    Removing the last group is allowed on every deployment shape. Where Apchi owns the mounts
    it takes them away and Trino falls back to its legacy manager; where the deployment owns
    the pointer Apchi writes that fallback out instead (#125). Either way the Cluster ends up
    behaving the same, so there is nothing here to refuse.

    To take **every** Section back to one Snapshot instead of just this one, use
    `POST /candidate/rollback` -- a Full Rollback.
    """
    candidate = await store.load()
    section.delete_group(candidate.resources(GROUPS), path)
    await store.save(candidate)


@router.post(
    "/revert",
    response_model=RevertEffect,
    summary="Revert one Section to an earlier Snapshot",
    dependencies=[OperatorMutationAllowed],
)
async def revert(
    request: RevertRequest,
    store: CandidateStoreDep,
    snapshots: SnapshotStoreDep,
    cluster: ClusterDep,
) -> RevertEffect:
    return await section_revert(store, snapshots, cluster, GROUPS, request.snapshot)

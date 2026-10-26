"""Catalogs. Every change lands in the Configuration Candidate and reaches nothing
else -- not Trino, not Kubernetes. Apply is what makes a change real."""

from typing import Annotated

from fastapi import APIRouter, Body, status

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

#: Named examples, because the interesting thing about creating a Catalog is which of these
#: three shapes it is -- and a single body cannot show that.
CREATE_EXAMPLES = {
    "postgresql": {
        "summary": "A PostgreSQL catalog",
        "description": (
            "A curated connector, so every property here is checked against a schema and a "
            "typo like `connection-uri` is refused now rather than at Apply."
        ),
        "value": {
            "name": "finance_pg",
            "connector": "postgresql",
            "properties": {
                "connection-url": (
                    "jdbc:postgresql://pg.finance.svc.cluster.local:5432/ledger?sslmode=verify-full"
                ),
                "connection-user": "trino_reader",
                "connection-password": "the-password-goes-here",
            },
        },
    },
    "certificate": {
        "summary": "Presenting a Client Certificate",
        "description": (
            "Name a Client Certificate and Apchi puts it into the properties the way this "
            "connector expects, so no path is ever typed. The `sslmode` is yours to choose "
            "and is required: a certificate does nothing until the connection is told to "
            "use TLS, and Apchi will not pick between `require` and `verify-full` for you."
        ),
        "value": {
            "name": "finance_pg",
            "connector": "postgresql",
            "certificate": "finance-client",
            "properties": {
                "connection-url": (
                    "jdbc:postgresql://pg.finance.svc.cluster.local:5432/ledger?sslmode=verify-full"
                ),
                "connection-user": "trino_reader",
            },
        },
    },
    "uncurated": {
        "summary": "A connector Apchi has no schema for",
        "description": (
            "Properties pass through unchecked and the Catalog comes back with "
            "`supported: false`, so nothing but the ephemeral coordinator at Validation "
            "will judge them. The connector itself is still checked: it has to be one this "
            "Trino actually has."
        ),
        "value": {
            "name": "events",
            "connector": "clickhouse",
            "properties": {
                "connection-url": "jdbc:clickhouse://clickhouse.svc.cluster.local:8123/",
                "connection-user": "trino_reader",
            },
        },
    },
}

#: Three, and they exist to make one distinction visible: omitted, present, and null are
#: three different instructions. No single body can show that, and getting it wrong is what
#: made a Catalog's certificate set-once -- removable only by dropping the Catalog, which in
#: Trino is a DROP CATALOG (#155).
UPDATE_EXAMPLES = {
    "rotate": {
        "summary": "Rotate the password, keep everything else",
        "description": (
            "`properties` **replaces** the whole map rather than merging into it, so every "
            "property the Catalog should keep has to be here. `connector` and `certificate` "
            "are omitted, which means unchanged."
        ),
        "value": {
            "properties": {
                "connection-url": (
                    "jdbc:postgresql://pg.finance.svc.cluster.local:5432/ledger?sslmode=verify-full"
                ),
                "connection-user": "trino_reader",
                "connection-password": "the-new-password",
            }
        },
    },
    "clear-certificate": {
        "summary": "Stop presenting a Client Certificate",
        "description": (
            "`null` clears it, where omitting the field would keep it. The certificate "
            "itself is untouched and stays available to other Catalogs."
        ),
        "value": {"certificate": None},
    },
    "connector": {
        "summary": "Change the connector",
        "description": (
            "Refused if this Trino has no such connector. It cannot be cleared either: "
            "a Catalog without a connector is not a Catalog, so sending null is refused and "
            "omitting the field keeps the current one."
        ),
        "value": {"connector": "mysql"},
    },
}


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
    """Stages into the Candidate; it does not apply. An ordinary POST /applies follows,
    like any other edit.

    The response says which catalogs an Apply would drop, and that reverting one Section
    while the others stay put produces a configuration that has never run.

    To take **every** Section back to one Snapshot instead of just this one, use
    `POST /candidate/rollback` -- a Full Rollback.
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
async def create_catalog(
    write: Annotated[CatalogWrite, Body(openapi_examples=CREATE_EXAMPLES)],
    store: CandidateStoreDep,
    trino: TrinoDep,
) -> Catalog:
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
    name: str,
    update: Annotated[CatalogUpdate, Body(openapi_examples=UPDATE_EXAMPLES)],
    store: CandidateStoreDep,
    trino: TrinoDep,
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

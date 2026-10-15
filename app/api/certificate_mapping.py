"""The Certificate Mapping Pattern: one rule, so one resource rather than a collection.

Every change lands in the Configuration Candidate and reaches nothing else. Applying one
restarts the coordinator, because Trino parses the rule list when the authenticator is built
and never again.
"""

from fastapi import APIRouter, status

from app.api.deps import (
    CandidateStoreDep,
    ClusterDep,
    OperatorMutationAllowed,
    SnapshotStoreDep,
)
from app.pipeline.recovery import RevertEffect, RevertRequest, section_revert
from app.sections.certificate_mapping import SECTION as MAPPING
from app.sections.certificate_mapping import section
from app.sections.certificate_mapping.model import CertificateMapping, CertificateMappingWrite

router = APIRouter(prefix="/certificate-mapping", tags=["certificate mapping"])


@router.get("", response_model=CertificateMapping, summary="The staged Certificate Mapping Pattern")
async def get_certificate_mapping(store: CandidateStoreDep) -> CertificateMapping:
    return section.get_mapping((await store.load()).resources(MAPPING))


@router.put(
    "",
    response_model=CertificateMapping,
    summary="Set the Certificate Mapping Pattern",
    dependencies=[OperatorMutationAllowed],
)
async def set_certificate_mapping(
    write: CertificateMappingWrite, store: CandidateStoreDep
) -> CertificateMapping:
    candidate = await store.load()
    saved = section.set_mapping(candidate.resources(MAPPING), write)
    await store.save(candidate)
    return saved


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove the Certificate Mapping Pattern",
    dependencies=[OperatorMutationAllowed],
)
async def clear_certificate_mapping(store: CandidateStoreDep) -> None:
    """Removing it does not remove the file. Trino's authenticator refuses to start without
    one, so what Apchi writes instead is the rule that leaves every name as presented --
    which is how Trino behaves with no mapping configured at all."""
    candidate = await store.load()
    section.clear_mapping(candidate.resources(MAPPING))
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
    return await section_revert(store, snapshots, cluster, MAPPING, request.snapshot)

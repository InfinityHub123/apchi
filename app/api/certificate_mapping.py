"""The Certificate Mapping Pattern: one rule, so one resource rather than a collection.

Every change lands in the Configuration Candidate and reaches nothing else. Applying one
restarts the coordinator, because Trino parses the rule list when the authenticator is built
and never again.
"""

from typing import Annotated

from fastapi import APIRouter, Body, status

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

#: Three, and what they vary is how much of the subject the pattern pins down -- which is
#: the whole decision here, because a pattern that is too loose maps certificates from the
#: wrong team to a Trino Identity that has their grants.
#:
#: The string the pattern is matched against is **Trino's** rendering of the certificate
#: subject, which is the JDK's: the RDNs reversed so the CN comes first, joined by a comma
#: and a space. A certificate whose subject Apchi reports as
#: `C=GB,O=Acme,OU=analytics,CN=trino-reader.finance.acme.internal` is presented to these
#: patterns as
#:
#:     CN=trino-reader.finance.acme.internal, OU=analytics, O=Acme, C=GB
#:
#: Those are two spellings of one name, and a pattern written against the first never
#: matches. See section 13.3.
SET_EXAMPLES = {
    "common-name": {
        "summary": "Take the identity from the CN",
        "description": (
            "The common case: everything up to the first comma is the CN, and `$1` is that "
            "capture. Any certificate the Cluster's truststore accepts maps, so this is the "
            "right pattern when the truststore is the access decision."
        ),
        "value": {"pattern": "CN=([^,]+).*", "user": "$1"},
    },
    "scoped": {
        "summary": "Only certificates from one team",
        "description": (
            "Pins the OU as well, so a certificate from another unit matches nothing here. "
            "That is not a denial -- Apchi's generated file ends in a rule that leaves an "
            "unmatched name as presented, because what a caller may *do* is Permissions' "
            "answer, not this Section's."
        ),
        "value": {"pattern": "CN=([^,]+), OU=analytics, .*", "user": "$1"},
    },
    "folded": {
        "summary": "Fold the derived identity to lower case",
        "description": (
            "For a CA that issues mixed-case names while the grants in Permissions are "
            "written in lower case. Trino folds after the substitution, so `case` applies "
            "to the result of `user` rather than to the subject."
        ),
        "value": {"pattern": "CN=([^,]+), OU=analytics, .*", "user": "$1", "case": "lower"},
    },
}


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
    write: Annotated[CertificateMappingWrite, Body(openapi_examples=SET_EXAMPLES)],
    store: CandidateStoreDep,
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
    which is how Trino behaves with no mapping configured at all.
    To take **every** Section back to one Snapshot instead of just this one, use
    `POST /candidate/rollback` -- a Full Rollback.
    """
    candidate = await store.load()
    section.clear_mapping(candidate.resources(MAPPING))
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
    return await section_revert(store, snapshots, cluster, MAPPING, request.snapshot)

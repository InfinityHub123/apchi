"""Client Certificates: what Trino presents when it connects outward.

Upload is the whole interface. An Operator drags a ZIP holding a certificate and its key;
Apchi works out which member is which, proves they are a pair, normalises the key and stores
it under a name. Nobody converts formats and nobody types a path.

Nothing here ever returns private key material.
"""

from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile, status

from app.api.deps import (
    CandidateStoreDep,
    ClusterDep,
    OperatorMutationAllowed,
    SettingsDep,
    SnapshotStoreDep,
)
from app.api.errors import Conflict, UnprocessablePayload
from app.pipeline.recovery import RevertEffect, RevertRequest, section_revert
from app.pipeline.references import certificates_in_use
from app.sections.client_certificates import SECTION as CERTIFICATES
from app.sections.client_certificates import section
from app.sections.client_certificates.bundle import BundleProblem
from app.sections.client_certificates.bundle import read as read_bundle
from app.sections.client_certificates.model import CertificateName, ClientCertificate, Status

router = APIRouter(prefix="/certificates", tags=["client certificates"])

#: An archive larger than this is not a certificate bundle, and reading it would be work
#: done on behalf of whoever sent it.
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024


@router.get("", response_model=list[ClientCertificate], summary="The staged certificates")
async def list_certificates(
    store: CandidateStoreDep,
    settings: SettingsDep,
    certificate_status: Annotated[
        Status | None,
        Query(alias="status", description="Only certificates in this state."),
    ] = None,
) -> list[ClientCertificate]:
    """Expiry is first-class: `?status=expiring` is how an Operator finds what to renew
    before a Catalog stops connecting."""
    return section.list_certificates(
        (await store.load()).resources(CERTIFICATES),
        settings.certificate_expiring_within_days,
        certificate_status,
    )


@router.post(
    "",
    response_model=ClientCertificate,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a certificate and its key as a ZIP",
    dependencies=[OperatorMutationAllowed],
)
async def upload_certificate(
    store: CandidateStoreDep,
    settings: SettingsDep,
    name: Annotated[CertificateName, Form(description="What Catalogs will reference it by.")],
    archive: Annotated[UploadFile, File(description="A ZIP holding the certificate and key.")],
) -> ClientCertificate:
    """Uploading under a name that is already staged replaces it, which is how renewal
    works: the Catalogs referencing it reference the name."""
    content = await archive.read()
    if len(content) > _MAX_ARCHIVE_BYTES:
        raise UnprocessablePayload(
            f"That archive is {len(content)} bytes. A certificate bundle is a few kilobytes.",
            details=[{"property": "archive", "problem": "too large to be a certificate bundle"}],
        )
    try:
        bundle = read_bundle(content)
    except BundleProblem as exc:
        raise UnprocessablePayload(
            str(exc), details=[{"property": "archive", "problem": str(exc)}]
        ) from exc

    candidate = await store.load()
    saved = section.store_certificate(candidate.resources(CERTIFICATES), name, bundle, settings)
    await store.save(candidate)
    return saved


@router.get("/{name}", response_model=ClientCertificate, summary="One certificate")
async def get_certificate(
    name: str, store: CandidateStoreDep, settings: SettingsDep
) -> ClientCertificate:
    return section.get_certificate(
        (await store.load()).resources(CERTIFICATES),
        name,
        settings.certificate_expiring_within_days,
    )


@router.delete(
    "/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a certificate",
    dependencies=[OperatorMutationAllowed],
)
async def delete_certificate(name: str, store: CandidateStoreDep) -> None:
    """Refused while a Catalog still presents it. Removing it would leave that Catalog
    pointing at a file that is about to disappear, and the failure would surface as a
    connection error with nothing pointing back at this request."""
    candidate = await store.load()
    section.get_certificate(candidate.resources(CERTIFICATES), name, 0)
    if used_by := certificates_in_use(candidate.sections, name):
        raise Conflict(
            f"Client certificate {name!r} is still presented by {', '.join(used_by)}. "
            "Change those Catalogs first, or they would be left pointing at a file that "
            "is no longer there."
        )
    section.delete_certificate(candidate.resources(CERTIFICATES), name)
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
    return await section_revert(store, snapshots, cluster, CERTIFICATES, request.snapshot)

"""Client Certificates: what Trino presents when it connects outward.

Upload is the whole interface. An Operator drags a ZIP holding a certificate and its key;
Apchi works out which member is which, proves they are a pair, normalises the key and stores
it under a name. Nobody converts formats and nobody types a path.

Nothing here ever returns private key material.
"""

from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

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
from app.sections.client_certificates.bundle import Bundle, BundleProblem
from app.sections.client_certificates.bundle import read as read_bundle
from app.sections.client_certificates.model import CertificateName, ClientCertificate, Status

router = APIRouter(prefix="/certificates", tags=["client certificates"])

#: An archive larger than this is not a certificate bundle, and reading it would be work
#: done on behalf of whoever sent it.
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024


class BundleReading(BaseModel):
    """What Apchi made of an archive, having stored nothing."""

    model_config = ConfigDict(extra="forbid")

    certificate_member: str = Field(
        description=(
            "Which member of the ZIP was taken as the certificate. Apchi identifies members "
            "by what they parse as and never by their name, so this is the only way somebody "
            "notices they zipped the issuing CA instead of their leaf."
        )
    )
    key_member: str = Field(description="Which member was taken as the private key.")
    conversions: list[str] = Field(
        description=(
            "What Apchi would change on the way in, in prose -- a DER certificate stored as "
            "PEM, a PKCS#1 key stored as PKCS#8. Empty when nothing is converted, which is "
            "its own answer rather than missing information."
        )
    )
    certificate: ClientCertificate = Field(
        description="What would be staged: subject, issuer, validity and expiry status."
    )
    replaces: ClientCertificate | None = Field(
        description=(
            "The certificate already staged under this name, which this one would replace. "
            "Null when the name is new. **This is the field the endpoint exists for**: "
            "uploading under an existing name replaces it immediately, so comparing the two "
            "expiries is how somebody confirms a renewal is a renewal rather than a mistake."
        )
    )
    used_by: list[str] = Field(
        description=(
            "Catalogs that currently present this name. Replacing the certificate re-points "
            "every one of them, which is how renewal is meant to work and is also what makes "
            "a wrong upload reach further than the certificate itself."
        )
    )


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


async def _read_archive(archive: UploadFile) -> Bundle:
    """The archive understood, or a 422 saying what is wrong with it.

    Shared by the upload and the inspection so the two cannot disagree about what a bundle
    is -- which is the whole claim the inspection makes.
    """
    content = await archive.read()
    if len(content) > _MAX_ARCHIVE_BYTES:
        raise UnprocessablePayload(
            f"That archive is {len(content)} bytes. A certificate bundle is a few kilobytes.",
            details=[{"property": "archive", "problem": "too large to be a certificate bundle"}],
        )
    try:
        return read_bundle(content)
    except BundleProblem as exc:
        raise UnprocessablePayload(
            str(exc), details=[{"property": "archive", "problem": str(exc)}]
        ) from exc


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
    name: Annotated[
        CertificateName,
        Form(
            description=(
                "What Catalogs will reference it by. A Catalog names this, never a path, "
                "so renewing a certificate under the same name re-points every Catalog "
                "using it."
            ),
            examples=["finance-client"],
        ),
    ],
    archive: Annotated[
        UploadFile,
        File(
            description=(
                "A ZIP holding the certificate and its private key, in any order and under "
                "any member names -- Apchi works out which member is which and proves they "
                "are a pair. PEM or DER, and an encrypted key needs its passphrase removed "
                "first."
            )
        ),
    ],
) -> ClientCertificate:
    """Uploading under a name that is already staged replaces it, which is how renewal
    works: the Catalogs referencing it reference the name."""
    bundle = await _read_archive(archive)
    candidate = await store.load()
    saved = section.store_certificate(candidate.resources(CERTIFICATES), name, bundle, settings)
    await store.save(candidate)
    return saved


@router.post(
    "/inspect",
    response_model=BundleReading,
    summary="Read an archive without staging it",
    dependencies=[OperatorMutationAllowed],
)
async def inspect_certificate(
    store: CandidateStoreDep,
    settings: SettingsDep,
    name: Annotated[
        CertificateName,
        Form(description="The name it would be stored under.", examples=["finance-client"]),
    ],
    archive: Annotated[UploadFile, File(description="The same ZIP `POST /certificates` takes.")],
) -> BundleReading:
    """What the upload would do, having done none of it.

    A separate route rather than a flag on the upload, deliberately. The other previews in
    this API take `dry_run` in a JSON body, where an unknown field is refused and a
    misspelling is a 422 -- but this endpoint takes multipart form data, and an unknown form
    field cannot be refused the same way. A mistyped flag would be ignored and the
    certificate staged for real, which is the one outcome somebody asking for a preview is
    trying to avoid. A path with no save in it cannot do that whatever is misspelled.

    It refuses in exactly the cases the upload refuses, because the half of
    `store_certificate` that validates is the half this calls. An inspection that said yes
    where the upload says no would be worse than none at all.
    """
    reading = await _read_archive(archive)
    candidate = await store.load()
    stored = candidate.resources(CERTIFICATES)
    return BundleReading(
        certificate_member=reading.certificate_member,
        key_member=reading.key_member,
        conversions=list(reading.conversions),
        certificate=section.check_certificate(stored, name, reading, settings),
        replaces=(
            section.get_certificate(stored, name, settings.certificate_expiring_within_days)
            if name in stored
            else None
        ),
        used_by=certificates_in_use(candidate.sections, name),
    )


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
    connection error with nothing pointing back at this request.
    To take **every** Section back to one Snapshot instead of just this one, use
    `POST /candidate/rollback` -- a Full Rollback.
    """
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
    summary="Revert one Section to an earlier Snapshot",
    dependencies=[OperatorMutationAllowed],
)
async def revert(
    request: RevertRequest,
    store: CandidateStoreDep,
    snapshots: SnapshotStoreDep,
    cluster: ClusterDep,
) -> RevertEffect:
    return await section_revert(store, snapshots, cluster, CERTIFICATES, request.snapshot)

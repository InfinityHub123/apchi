"""What an Operator sees of a Client Certificate.

Never the private key. It is in the Candidate because a Section Revert has to be able to put
it back (ADR-0005), and the redaction filter keeps it out of the logs, but nothing returns it:
an API that hands back key material makes every reader of every response a place it can leak
from.

Everything else is derived from the certificate rather than stored beside it, so the metadata
cannot drift from the bytes Trino will present.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

#: A name an Operator chooses, and a filename Trino reads. Constrained to what is safe in
#: both: a catalog's connector properties will carry it as a path.
CertificateName = Annotated[
    str, StringConstraints(min_length=1, max_length=63, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
]

Status = Literal["valid", "expiring", "expired"]


class ClientCertificate(BaseModel):
    """A certificate as it is listed and returned."""

    model_config = ConfigDict(extra="forbid")

    name: CertificateName
    common_name: str | None = Field(description="The subject's CN, if it has one.")
    subject: str = Field(description="Who the certificate identifies.")
    issuer: str = Field(description="Who signed it.")
    not_before: datetime
    not_after: datetime = Field(description="When it stops being accepted.")
    status: Status = Field(
        description="valid, expiring within the configured window, or already expired."
    )
    fingerprint: str = Field(description="SHA-256 of the certificate, for matching it elsewhere.")


def _common_name(subject: x509.Name) -> str | None:
    attributes = subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if not attributes:
        return None
    value = attributes[0].value
    return value if isinstance(value, str) else value.decode()


def describe(name: str, certificate_pem: str, expiring_within_days: int) -> ClientCertificate:
    """Read the certificate and say what an Operator needs to know about it.

    Parsed on every read rather than cached at upload, because the certificate is the truth
    and a cached copy of its expiry is a second truth that can be wrong.
    """
    certificate = x509.load_pem_x509_certificate(certificate_pem.encode())
    expires = certificate.not_valid_after_utc
    now = datetime.now(UTC)
    if expires <= now:
        status: Status = "expired"
    elif (expires - now).days <= expiring_within_days:
        status = "expiring"
    else:
        status = "valid"
    return ClientCertificate(
        name=name,
        common_name=_common_name(certificate.subject),
        subject=certificate.subject.rfc4514_string(),
        issuer=certificate.issuer.rfc4514_string(),
        not_before=certificate.not_valid_before_utc,
        not_after=expires,
        status=status,
        fingerprint=certificate.fingerprint(hashes.SHA256()).hex(),
    )

"""Reading an uploaded ZIP.

Everything here is about refusing badly rather than storing badly. A certificate that is not
a pair with its key, or a key Apchi cannot read, becomes a catalog that cannot connect --
and the failure would surface later, somewhere else, as a connection error with no obvious
cause. So the archive is understood at upload time or refused at upload time.

Members are identified by what they parse as, never by their extension. A ZIP exported from
one tool calls the key `client-key.pem` and from another `privkey.pk8`; both are keys, and
neither name is worth trusting.
"""

import io
import zipfile
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes, PublicKeyTypes

#: Metadata that could only be nonsense: a member so large it is not a certificate, and a
#: count so high the archive is not a bundle. Both are cheap guards against a ZIP designed
#: to be expensive to read rather than to be read.
_MAX_MEMBER_BYTES = 1024 * 1024
_MAX_MEMBERS = 64


class BundleProblem(Exception):
    """The archive is not a certificate and its key, and here is why."""


@dataclass(frozen=True)
class Bundle:
    """What was in the ZIP, in the form Apchi stores.

    The key is normalised to PKCS#8 PEM whatever arrived: uploads come as PKCS#1
    (`BEGIN RSA PRIVATE KEY`), as PKCS#8, and as DER, and a Section that stored each of them
    as they came would make every consumer handle all three.
    """

    certificate: x509.Certificate
    certificate_pem: str
    private_key_pem: str


def _members(archive: bytes) -> list[bytes]:
    try:
        zipped = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as exc:
        raise BundleProblem(f"That is not a ZIP archive Apchi can read: {exc}") from exc

    entries = [
        info
        for info in zipped.infolist()
        if not info.is_dir()
        and not info.filename.split("/")[-1].startswith(".")
        # Archives from macOS carry a parallel __MACOSX tree of resource forks, which parse
        # as nothing and would only add noise to the refusal messages.
        and not info.filename.startswith("__MACOSX/")
    ]
    if not entries:
        raise BundleProblem("The ZIP archive is empty.")
    if len(entries) > _MAX_MEMBERS:
        raise BundleProblem(
            f"The ZIP archive holds {len(entries)} files. A certificate bundle holds a "
            "certificate and a key."
        )
    contents = []
    for info in entries:
        if info.file_size > _MAX_MEMBER_BYTES:
            raise BundleProblem(
                f"{info.filename!r} is {info.file_size} bytes, which is far larger than any "
                "certificate or key."
            )
        contents.append(zipped.read(info))
    return contents


def _as_certificate(data: bytes) -> x509.Certificate | None:
    for load in (x509.load_pem_x509_certificate, x509.load_der_x509_certificate):
        try:
            return load(data)
        except Exception:
            continue
    return None


def _as_private_key(data: bytes) -> PrivateKeyTypes | None:
    """None when this member is not a key Apchi can use.

    An encrypted key is the one case worth distinguishing, because it is not malformed -- it
    is a key with a passphrase Apchi was not given, and telling an Operator "not a key"
    would send them looking for the wrong problem.
    """
    for load in (serialization.load_pem_private_key, serialization.load_der_private_key):
        try:
            return load(data, password=None)
        except TypeError as exc:
            raise BundleProblem(
                "The private key is encrypted. Apchi holds no passphrase for it; upload it "
                "without one."
            ) from exc
        except Exception:
            continue
    return None


def _public_bytes(public: PublicKeyTypes) -> bytes:
    """A public key in one comparable form, whatever kind of key it is."""
    return public.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def read(archive: bytes) -> Bundle:
    """The certificate and key in an uploaded ZIP, or a refusal saying what is wrong.

    The pair check doubles as the way the leaf certificate is chosen: a bundle often carries
    the issuing CA as well, and the certificate Trino should present is the one whose public
    key matches the private key. Anything else in the archive is not stored -- a chain is
    configuration of its own, and guessing at it would put key material in files nobody
    asked for.
    """
    certificates: list[x509.Certificate] = []
    keys: list[PrivateKeyTypes] = []
    for data in _members(archive):
        if (certificate := _as_certificate(data)) is not None:
            certificates.append(certificate)
        elif (key := _as_private_key(data)) is not None:
            keys.append(key)

    if not certificates:
        raise BundleProblem("The ZIP archive holds no certificate Apchi can read.")
    if not keys:
        raise BundleProblem("The ZIP archive holds no private key Apchi can read.")
    if len(keys) > 1:
        raise BundleProblem(
            f"The ZIP archive holds {len(keys)} private keys. Apchi cannot tell which one "
            "belongs to the certificate; upload one certificate and its key."
        )

    key = keys[0]
    wanted = _public_bytes(key.public_key())
    for certificate in certificates:
        if _public_bytes(certificate.public_key()) == wanted:
            return Bundle(
                certificate=certificate,
                certificate_pem=certificate.public_bytes(serialization.Encoding.PEM).decode(),
                private_key_pem=key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode(),
            )

    raise BundleProblem(
        "The certificate and the private key in that archive are not a pair. A certificate "
        "presented with the wrong key is a connection that fails at handshake."
    )

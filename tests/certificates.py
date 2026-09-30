"""Building certificate bundles for tests.

Generated rather than committed. A test that feeds in a fixture binary makes the reader open
a hex editor to find out what the case is; a test that builds its own says so in the test.
"""

import datetime
import io
import zipfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def key_pair(
    common_name: str = "finance.clients.example.com", valid_for_days: int = 365
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """A self-signed certificate and its key. `valid_for_days` may be negative, which is how
    an expired certificate is made."""
    # 2048 rather than 4096: every test pays for this, and nothing here is protecting data.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Acme"),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=2))
        .not_valid_after(now + datetime.timedelta(days=valid_for_days))
        .sign(key, hashes.SHA256())
    )
    return certificate, key


def certificate_pem(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


def key_pem(
    key: rsa.RSAPrivateKey,
    fmt: serialization.PrivateFormat = serialization.PrivateFormat.PKCS8,
    passphrase: bytes | None = None,
) -> bytes:
    encryption: serialization.KeySerializationEncryption = serialization.NoEncryption()
    if passphrase is not None:
        encryption = serialization.BestAvailableEncryption(passphrase)
    return key.private_bytes(serialization.Encoding.PEM, fmt, encryption)


def zipped(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def bundle(common_name: str = "finance.clients.example.com", valid_for_days: int = 365) -> bytes:
    """The ordinary case: a ZIP holding a certificate and its key."""
    certificate, key = key_pair(common_name, valid_for_days)
    return zipped({"client.crt": certificate_pem(certificate), "client.key": key_pem(key)})

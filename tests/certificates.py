"""Building certificate bundles for tests.

Generated rather than committed. A test that feeds in a fixture binary makes the reader open
a hex editor to find out what the case is; a test that builds its own says so in the test.
"""

import datetime
import io
import zipfile
from ipaddress import ip_address
from typing import NamedTuple

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


#: A certificate authority and the two certificates it issues, as PEM bytes.
class TlsChain(NamedTuple):
    ca: bytes
    server: bytes
    server_key: bytes
    client: bytes
    client_key: bytes


def tls_chain(server_name: str = "localhost", client_name: str = "apchi") -> TlsChain:
    """A CA, a server certificate for `server_name` and a client certificate.

    Unlike `key_pair`, these are built to survive a real TLS handshake, which took three
    rounds to discover: OpenSSL 3 rejects a chain whose CA has no Subject Key Identifier
    ("Missing Authority Key Identifier"), whose leaf has no Authority Key Identifier, and
    whose CA has no `keyUsage` with `keyCertSign` ("CA cert does not include key usage
    extension"). `key_pair`'s certificates are only ever parsed, never verified, so none of
    this surfaced there.
    """
    now = datetime.datetime.now(datetime.UTC)

    def name(common_name: str) -> x509.Name:
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca = (
        x509.CertificateBuilder()
        .subject_name(name("Apchi Test CA"))
        .issuer_name(name("Apchi Test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    def issued(common_name: str, server: bool) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        builder = (
            x509.CertificateBuilder()
            .subject_name(name(common_name))
            .issuer_name(ca.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
            )
        )
        if server:
            # Verification matches the SAN, not the CN, so a certificate without one is
            # rejected however its subject reads.
            builder = builder.add_extension(
                x509.SubjectAlternativeName(
                    [x509.DNSName(common_name), x509.IPAddress(ip_address("127.0.0.1"))]
                ),
                critical=False,
            )
        return builder.sign(ca_key, hashes.SHA256()), key

    server_certificate, server_key = issued(server_name, server=True)
    client_certificate, client_key = issued(client_name, server=False)
    return TlsChain(
        ca=certificate_pem(ca),
        server=certificate_pem(server_certificate),
        server_key=key_pem(server_key),
        client=certificate_pem(client_certificate),
        client_key=key_pem(client_key),
    )


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

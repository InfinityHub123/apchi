"""What Apchi trusts when it makes an outbound TLS connection, and what it presents.

One bundle for all four destinations -- the Trino coordinator, MongoDB, the OIDC provider
and the alert webhook (#187). Apchi reaches them through three HTTP libraries, and each
takes its trust configuration in a different shape, so this is the single place that knows
the difference.

**A configured CA is added to the public trust store, not substituted for it**, and that is
not what any of the three libraries do on their own. A CA *path* handed to PyMongo or to the
Trino client's `requests` session yields a context trusting exactly that one CA -- measured
at 1, against 147 for a default context with the same CA loaded into it. Only httpx can be
given a context built the other way.

So the bundle is concatenated here instead, once, and every client is handed the result.
Two things go wrong without it. An internal OIDC provider with a private CA would stop a
public alert webhook from verifying, which is a TLS failure against a host nobody
configured. And Trino, which is one destination reached through two libraries, would have
its liveness check trust the public store while its queries did not.

Concatenating in Apchi rather than in an initContainer, which the ticket also offered: it is
a few lines against a container on every pod, it is covered by tier 1, and it works for a
deployment that does not use the chart.

**Everything here is read once, at startup, and the mounted file is never read again** (#193).
That is a decision and not an oversight: a rotation therefore takes effect when the pod
restarts. See `presented` for why the alternative was worse, and `rotated` for how an Admin
finds out.

Both the bundle and the copies are written with `mkstemp`, so this module needs a **writable
temporary directory**. A container hardened with `readOnlyRootFilesystem: true` would fail at
startup; the chart does not set it, and a deployment that wants to would have to mount an
`emptyDir` over the temporary directory.
"""

import hashlib
import logging
import ssl
from contextlib import suppress
from functools import cache
from pathlib import Path
from tempfile import mkstemp

import certifi
from cryptography import x509

logger = logging.getLogger(__name__)

#: The digest of what each mounted file held when Apchi read it, keyed by the mounted path.
#: Written once per path, as the material is loaded, and compared against the mount again by
#: `rotated` -- which is the only thing that ever looks at these files twice.
_loaded: dict[str, str] = {}

#: Where a PEM's first certificate starts. A key file has none, and MongoDB's
#: `tlsCertificateKeyFile` holds a certificate *and* a key in one file, so neither "the whole
#: file is a certificate" nor "this file is not a certificate" is safe to assume.
_CERTIFICATE_HEADER = b"-----BEGIN CERTIFICATE-----"


def _read(path: str) -> bytes:
    """`path`'s bytes, remembering their digest so a later rotation can be noticed."""
    content = Path(path).read_bytes()
    if path not in _loaded:
        _loaded[path] = hashlib.sha256(content).hexdigest()
        _log_certificate(path, content)
    return content


def _log_certificate(path: str, content: bytes) -> None:
    """The subject and expiry of the certificate in `content`, if it holds one.

    Logged because a deployment that rotates by restarting makes the restart the moment an
    Admin wants to see what was loaded and when it dies; there is no other way to ask. A file
    with no certificate in it is a private key, and says nothing here.
    """
    start = content.find(_CERTIFICATE_HEADER)
    if start == -1:
        return
    try:
        certificate = x509.load_pem_x509_certificate(content[start:])
    except ValueError:
        # Not fatal: whichever client was given this file will reject it far more precisely
        # than this could, and failing here would turn a bad certificate into a crash with
        # no destination named.
        logger.warning("could not parse a certificate from %s", path)
        return
    logger.info(
        "loaded certificate from %s, subject %s, expires %s",
        path,
        certificate.subject.rfc4514_string(),
        certificate.not_valid_after_utc.isoformat(),
        extra={"certificate_path": path, "expires": certificate.not_valid_after_utc.isoformat()},
    )


@cache
def bundled_ca(ca_file: str | None) -> str | None:
    """The public trust store and `ca_file` in one file, or None to leave a client's default.

    None in, None out, and nothing is written: an unconfigured Apchi keeps whatever each
    library trusts by default, which is `certifi` for httpx and the system store for
    PyMongo. Only a configured CA makes those two agree, and only then is it worth a file.

    Cached, so the bundle is built once per path for the life of the process. The result is
    a public certificate list and nothing secret, so it is written 0644 and left for the
    container's exit to remove.
    """
    if ca_file is None:
        return None
    if not Path(ca_file).exists():
        # Named here because this is the one chokepoint all four destinations pass through,
        # and because the path was assembled by the chart from a mount point and a Secret
        # key -- so the bare FileNotFoundError names a file nobody chose by that name.
        raise RuntimeError(
            f"Apchi cannot start: {ca_file} does not exist. It comes from ca_file -- under "
            "Helm, pod.caBundle naming a key that Secret does not hold."
        )
    handle, path = mkstemp(prefix="apchi-ca-", suffix=".pem")
    with open(handle, "wb") as bundle:
        bundle.write(Path(certifi.where()).read_bytes())
        bundle.write(b"\n")
        bundle.write(_read(ca_file))
    Path(path).chmod(0o644)
    return path


@cache
def presented(certificate_file: str | None, key_file: str | None) -> tuple[str | None, str | None]:
    """Private copies of the certificate and key Apchi presents, or `(None, None)`.

    **Copies, rather than the mounted files themselves**, because the two libraries that
    reach the coordinator read a certificate at different moments. The official Trino client
    hands `requests` the two *paths* and `requests` opens them at every TLS handshake, while
    httpx is given a context with the bytes already loaded into it. Against a mounted Secret
    those disagree the instant one is rotated -- measured against a server reporting the CN it
    was shown: the query session presented the new certificate and the liveness check the old
    one, for as long as the pod lived (#193).

    Both halves read one file Apchi owns instead, so they cannot diverge. The cost is that a
    rotation needs a pod restart, which was the decision: MongoDB holds its certificate as
    bytes in a context built when the client is, so it could not follow a reload without
    rebuilding the client -- and a CA that reloaded for Trino alone would split trust across
    destinations, which is the failure this module exists to prevent.

    The copies are 0600, which the mount itself cannot be: the kubelet ORs 0440 into every
    file's mode in a readOnly volume, so 0640 is the floor there (§13.2). So this puts a
    private key in the container's temporary directory for the life of the process, and it is
    a *more* restricted copy of a key already in the same filesystem -- the mounted one is
    0640 and root-owned. Left for the container's exit to remove, like the bundle.
    """
    if certificate_file is None or key_file is None:
        return None, None
    return _private_copy(certificate_file), _private_copy(key_file)


def _private_copy(path: str) -> str:
    content = _read(path)
    # mkstemp creates at 0600 by design, which is what a private key wants, so nothing is
    # chmodded here -- unlike the CA bundle, which has to be widened to 0644.
    handle, copy = mkstemp(prefix="apchi-presented-", suffix=".pem")
    with open(handle, "wb") as destination:
        destination.write(content)
    return copy


def recorded(path: str | None) -> str | None:
    """`path` unchanged, with what it holds remembered so `rotated` can notice a change.

    For a file a client opens itself and Apchi has no reason to copy: PyMongo's
    `tlsCertificateKeyFile`, which it loads into a context while validating its options. A
    missing file is left alone rather than raised on, because PyMongo's own failure names the
    setting that put it there and a second error here would only arrive first.
    """
    if path is None:
        return None
    with suppress(OSError):
        _read(path)
    return path


def verification(
    ca_file: str | None, certificate_file: str | None = None, key_file: str | None = None
) -> ssl.SSLContext | bool:
    """What httpx verifies a destination with, or True for its own default trust store.

    A context rather than paths because httpx deprecated both `verify=<path>` and `cert=` in
    favour of one built here (#188). `cafile=None` still loads the default CAs, so a client
    certificate presented to a publicly signed destination needs no bundle.

    The certificate paths are expected to be `presented`'s copies, not the mount: that is
    what makes this context and the Trino client's session agree (#193).
    """
    bundle = bundled_ca(ca_file)
    if bundle is None and certificate_file is None:
        return True
    context = ssl.create_default_context(cafile=bundle)
    if certificate_file is not None:
        context.load_cert_chain(certificate_file, key_file)
    return context


def rotated() -> list[str]:
    """The mounted paths whose contents are no longer what Apchi loaded.

    The one place anything here looks at a mounted file twice. Reported rather than acted on:
    a stale certificate is not a broken one -- Apchi keeps presenting what it loaded, and that
    works until the destination stops accepting it -- so refusing an Apply would block work
    that would have succeeded, and would do it in the middle of a correct rotation.
    """
    changed = []
    # A copy, because this runs inside an Apply and a client built lazily on another task
    # would otherwise be able to add an entry mid-iteration.
    for path, digest in list(_loaded.items()):
        try:
            current = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            changed.append(path)
            continue
        if current != digest:
            changed.append(path)
    return changed


def log_rotations() -> None:
    """Say so when a mounted certificate has changed under a running Apchi."""
    for path in rotated():
        logger.warning(
            "%s has changed since Apchi read it; restart Apchi to present it. Until then "
            "Apchi keeps using what it loaded at startup",
            path,
            extra={"certificate_path": path},
        )

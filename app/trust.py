"""What Apchi trusts when it makes an outbound TLS connection.

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
"""

import ssl
from functools import cache
from pathlib import Path
from tempfile import mkstemp

import certifi


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
        bundle.write(Path(ca_file).read_bytes())
    Path(path).chmod(0o644)
    return path


def verification(
    ca_file: str | None, certificate_file: str | None = None, key_file: str | None = None
) -> ssl.SSLContext | bool:
    """What httpx verifies a destination with, or True for its own default trust store.

    A context rather than paths because httpx deprecated both `verify=<path>` and `cert=` in
    favour of one built here (#188). `cafile=None` still loads the default CAs, so a client
    certificate presented to a publicly signed destination needs no bundle.
    """
    bundle = bundled_ca(ca_file)
    if bundle is None and certificate_file is None:
        return True
    context = ssl.create_default_context(cafile=bundle)
    if certificate_file is not None:
        context.load_cert_chain(certificate_file, key_file)
    return context

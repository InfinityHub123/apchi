"""One CA bundle, added to the public trust store rather than replacing it (#187).

The measurements here are the reason `app.trust` exists. Apchi reaches four destinations
through three HTTP libraries, and the two that take a CA *path* -- PyMongo and the Trino
client's `requests` session -- replace their trust store with it. Only httpx can be handed a
context built the other way. So the bundle is concatenated once and every client gets the
result, and these tests hold that property rather than the implementation.
"""

import http.server
import json
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path

import certifi
import pytest
from pymongo.ssl_support import get_ssl_context
from urllib3.util.ssl_ import create_urllib3_context

from app.adapters.mongo import _tls_options
from app.auth.oidc import OidcError, OidcProvider
from app.config import Environment, Settings
from app.pipeline.auto_rollback import _alert
from app.trust import bundled_ca, verification
from tests.certificates import tls_chain


def _subjects(context: ssl.SSLContext) -> list[str]:
    return [entry["subject"][-1][0][1] for entry in context.get_ca_certs()]


@pytest.fixture(scope="module")
def private_ca(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A CA no public store has heard of, which is the whole premise."""
    path = tmp_path_factory.mktemp("trust") / "ca.pem"
    path.write_bytes(tls_chain().ca)
    return str(path)


def test_nothing_configured_leaves_every_client_its_own_default(private_ca: str) -> None:
    """No file is written and no client is overridden. An unconfigured Apchi behaves exactly
    as it did before this existed, which is what keeps the default path unchanged."""
    assert bundled_ca(None) is None
    assert verification(None) is True


def test_a_configured_ca_is_added_to_the_public_store(private_ca: str) -> None:
    """The property #187 turns on. `load_verify_locations` on a default context adds; the
    `create_default_context(cafile=...)` form replaces, and the counts below are why that
    distinction is not a detail."""
    public = len(_subjects(ssl.create_default_context(cafile=certifi.where())))
    context = verification(private_ca)

    assert isinstance(context, ssl.SSLContext)
    subjects = _subjects(context)
    assert "Apchi Test CA" in subjects
    assert len(subjects) == public + 1


def test_the_two_libraries_that_take_a_path_would_otherwise_trust_only_that_ca(
    private_ca: str,
) -> None:
    """Not a test of Apchi, but of the premise Apchi is built on -- so that if PyMongo or
    urllib3 ever start adding instead of replacing, this says so rather than leaving a
    concatenation nobody can justify any more."""
    mongo_direct = get_ssl_context(None, None, private_ca, None, False, False, False, True)
    requests_direct = create_urllib3_context()
    requests_direct.load_verify_locations(cafile=private_ca)

    assert _subjects(mongo_direct) == ["Apchi Test CA"]
    assert _subjects(requests_direct) == ["Apchi Test CA"]


def test_the_bundle_a_path_taking_client_gets_holds_both(private_ca: str) -> None:
    """So handing the *bundle* to those same two libraries gets the additive behaviour the
    library itself will not give."""
    bundle = bundled_ca(private_ca)
    assert bundle is not None

    mongo_bundled = get_ssl_context(None, None, bundle, None, False, False, False, True)
    subjects = _subjects(mongo_bundled)
    assert "Apchi Test CA" in subjects
    assert len(subjects) > 1


def test_the_bundle_is_public_and_readable(private_ca: str) -> None:
    """0644 on purpose: a list of public certificates with no key in it. The chart mounts the
    CA volume with no defaultMode and no fsGroup for the same reason."""
    bundle = Path(str(bundled_ca(private_ca)))
    assert bundle.stat().st_mode & 0o777 == 0o644
    assert b"BEGIN CERTIFICATE" in bundle.read_bytes()


def test_the_bundle_is_built_once_per_path(private_ca: str) -> None:
    """Cached, so four clients asking for the same bundle do not write four files -- and so
    that the path stays stable for the life of the process, which `requests` and PyMongo
    hold on to."""
    assert bundled_ca(private_ca) == bundled_ca(private_ca)


def test_every_destination_reads_the_same_setting(private_ca: str) -> None:
    """One `ca_file` and not one per destination. Mongo takes the bundle as a path and the
    httpx callers take a context, but both come from the same configured file."""
    settings = Settings(environment=Environment.TEST, ca_file=private_ca)

    assert _tls_options(settings) == {"tlsCAFile": bundled_ca(private_ca)}
    assert isinstance(verification(settings.ca_file), ssl.SSLContext)


def test_a_client_certificate_without_a_ca_still_uses_the_public_store(
    private_ca: str,
) -> None:
    """`cafile=None` loads the default CAs, so a certificate presented to a publicly signed
    destination needs no bundle of its own."""
    chain = tls_chain()
    directory = Path(str(bundled_ca(private_ca))).parent
    certificate = directory / "client.pem"
    key = directory / "client-key.pem"
    certificate.write_bytes(chain.client)
    key.write_bytes(chain.client_key)

    context = verification(None, str(certificate), str(key))

    assert isinstance(context, ssl.SSLContext)
    assert len(_subjects(context)) > 1


class _Collector(http.server.BaseHTTPRequestHandler):
    """Records what reached it, so "the alert arrived" is an assertion and not an absence."""

    received: list[str] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        type(self).received.append(self.rfile.read(length).decode())
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        """Silent: the failing case here is meant to fail."""


@pytest.fixture
def webhook(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """An HTTPS webhook signed by a private CA, and that CA's path."""
    chain = tls_chain()
    certificate = tmp_path / "server.pem"
    certificate.write_bytes(chain.server)
    key = tmp_path / "server-key.pem"
    key.write_bytes(chain.server_key)
    ca = tmp_path / "webhook-ca.pem"
    ca.write_bytes(chain.ca)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certificate), str(key))
    server = http.server.HTTPServer(("127.0.0.1", 0), _Collector)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Collector.received = []
    try:
        yield f"https://localhost:{server.server_address[1]}/hook", str(ca)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_the_alert_webhook_verifies_with_the_configured_ca(
    webhook: tuple[str, str],
) -> None:
    """One of the two destinations #187 was actually about -- the others were already
    covered by #188 and #186. An internal webhook over HTTPS is unreachable without this."""
    url, ca = webhook
    settings = Settings(environment=Environment.TEST, alert_webhook_url=url, ca_file=ca)

    await _alert("apply-1", "a reason", settings)

    assert len(_Collector.received) == 1
    assert "Auto Rollback failed after Apply apply-1" in _Collector.received[0]


async def test_without_the_ca_the_alert_is_dropped_rather_than_raised(
    webhook: tuple[str, str],
) -> None:
    """`_alert` swallows its failures on purpose: a failure to alert must not mask the
    incident it was reporting. So the symptom of an untrusted webhook is silence, which is
    exactly why the CA has to be configurable rather than inferred."""
    url, _ = webhook
    settings = Settings(environment=Environment.TEST, alert_webhook_url=url)

    await _alert("apply-1", "a reason", settings)

    assert _Collector.received == []


async def test_an_oidc_provider_on_a_private_ca_can_be_discovered(
    https_provider: tuple[str, str],
) -> None:
    """The fourth destination, and the one where replacing the public store would have been
    worst: a provider's JWKS can sit on a different host entirely. Discovery over its own
    HTTPS is the honest test -- reading the context out of httpx would assert on private
    internals that could change under it without the wiring ever breaking."""
    issuer, ca = https_provider
    provider = OidcProvider(
        Settings(
            environment=Environment.TEST,
            ca_file=ca,
            oidc_issuer=issuer,
            oidc_client_id="apchi",
            oidc_redirect_url=f"{issuer}/callback",
        )
    )
    try:
        discovered = await provider.discovered()
    finally:
        await provider.close()

    assert discovered.jwks_uri == f"{issuer}/keys"


async def test_without_the_ca_discovery_fails_rather_than_degrading(
    https_provider: tuple[str, str],
) -> None:
    """§14's rule for this provider: nothing here degrades. An untrusted issuer is a refused
    login, not a login that skips verification."""
    issuer, _ = https_provider
    provider = OidcProvider(
        Settings(
            environment=Environment.TEST,
            oidc_issuer=issuer,
            oidc_client_id="apchi",
            oidc_redirect_url=f"{issuer}/callback",
        )
    )
    try:
        with pytest.raises(OidcError):
            await provider.discovered()
    finally:
        await provider.close()


class _Discovery(http.server.BaseHTTPRequestHandler):
    """The one document `OidcProvider.discovered()` reads, and nothing else."""

    issuer = ""

    def do_GET(self) -> None:
        body = json.dumps(
            {
                "issuer": type(self).issuer,
                "authorization_endpoint": f"{type(self).issuer}/auth",
                "token_endpoint": f"{type(self).issuer}/token",
                "jwks_uri": f"{type(self).issuer}/keys",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silent: one of the two cases below is meant to fail."""


@pytest.fixture
def https_provider(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """An OIDC issuer served over HTTPS signed by a private CA, and that CA's path."""
    chain = tls_chain()
    certificate = tmp_path / "idp.pem"
    certificate.write_bytes(chain.server)
    key = tmp_path / "idp-key.pem"
    key.write_bytes(chain.server_key)
    ca = tmp_path / "idp-ca.pem"
    ca.write_bytes(chain.ca)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certificate), str(key))
    server = http.server.HTTPServer(("127.0.0.1", 0), _Discovery)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    # The document's own `issuer` must equal the configured one, or the provider refuses it
    # for a different and correct reason.
    _Discovery.issuer = f"https://localhost:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Discovery.issuer, str(ca)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_missing_ca_names_the_setting_rather_than_only_the_path() -> None:
    """One chokepoint, one message. All four destinations go through `bundled_ca`, so the
    missing-file report belongs here rather than in each adapter -- and the path it names was
    assembled by the chart, so the bare FileNotFoundError names a file nobody chose."""
    with pytest.raises(RuntimeError, match="ca_file") as raised:
        bundled_ca("/nonexistent/ca.pem")

    assert "/nonexistent/ca.pem" in str(raised.value)
    assert "pod.caBundle" in str(raised.value)

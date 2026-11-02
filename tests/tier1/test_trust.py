"""One CA bundle, added to the public trust store rather than replacing it (#187).

The measurements here are the reason `app.trust` exists. Apchi reaches four destinations
through three HTTP libraries, and the two that take a CA *path* -- PyMongo and the Trino
client's `requests` session -- replace their trust store with it. Only httpx can be handed a
context built the other way. So the bundle is concatenated once and every client gets the
result, and these tests hold that property rather than the implementation.
"""

import asyncio
import http.server
import json
import logging
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import certifi
import httpx
import pytest
import requests
from cryptography import x509
from pymongo.ssl_support import get_ssl_context
from urllib3.util.ssl_ import create_urllib3_context

from app import trust
from app.adapters.mongo import _tls_options
from app.adapters.trino import Trino
from app.auth.oidc import OidcError, OidcProvider
from app.config import Environment, Settings
from app.pipeline.applies import TERMINAL
from app.pipeline.auto_rollback import _alert
from app.trust import bundled_ca, presented, verification
from tests.certificates import TlsChain, certificate_pem, key_pair, key_pem, tls_chain


def _subjects(context: ssl.SSLContext) -> list[str]:
    """The CAs a context has loaded, by common name.

    Only valid on a context built with an explicit `cafile`. A platform that hands OpenSSL a
    hashed CA **directory** instead loads its certificates on demand, and `get_ca_certs()`
    then reports an empty list however many are installed -- 0 for `capath=/etc/ssl/certs`
    against 146 for `cafile=/etc/ssl/cert.pem` on the same machine. So this must never be
    used to measure the *platform default* store, which is what failed in CI while passing
    locally.
    """
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


def test_a_client_certificate_without_a_ca_still_verifies_against_the_default_store(
    tmp_path: Path,
) -> None:
    """`cafile=None` loads the platform's own CAs, so a certificate presented to a publicly
    signed destination needs no bundle of its own.

    Asserted through `verify_mode` and not by counting `get_ca_certs()`, which cannot see a
    default store at all when the platform hands OpenSSL a **hashed CA directory** rather
    than a bundle file: those certificates are loaded on demand and never enumerated.
    `create_default_context(capath=...)` reports 0 where `cafile=...` reports 146, and that
    difference is the platform's, not the behaviour's -- it is why the first version of this
    test passed locally and failed in CI.
    """
    chain = tls_chain()
    certificate = tmp_path / "client.pem"
    certificate.write_bytes(chain.client)
    key = tmp_path / "client-key.pem"
    key.write_bytes(chain.client_key)

    context = verification(None, str(certificate), str(key))

    assert isinstance(context, ssl.SSLContext)
    # A verifying context, which is what distinguishes this from the `verify=False` shape:
    # a client certificate must never be the thing that turns verification off.
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    # And no bundle was built for it, so nothing replaced the platform's store.
    assert bundled_ca(None) is None


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


# --------------------------------------------------------------------------------------
# Rotation: everything above is read once, at startup, and these hold that it is read
# once by *both* libraries that reach the coordinator (#193).
# --------------------------------------------------------------------------------------


class _Identifier(http.server.BaseHTTPRequestHandler):
    """Reports the common name of the certificate the client presented.

    The only answer worth asserting on. Inspecting a client's own options says which path it
    was handed, never which bytes it put on the wire -- and the whole defect here was two
    libraries reading different bytes from the same path.
    """

    def do_GET(self) -> None:
        peer = self.connection.getpeercert() or {}
        common_name = "none"
        for rdn in peer.get("subject", ()):
            for attribute, value in rdn:
                if attribute == "commonName":
                    common_name = value
        body = common_name.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silent."""


class _Mount(NamedTuple):
    """A server that demands a certificate, and the mounted files Apchi is pointed at."""

    url: str
    ca: str
    certificate: str
    key: str
    chain: TlsChain

    def rotate(self) -> None:
        """Replace the mounted bytes in place, as the kubelet does on a Secret update."""
        Path(self.certificate).write_bytes(self.chain.rotated_client or b"")
        Path(self.key).write_bytes(self.chain.rotated_client_key or b"")


@pytest.fixture
def mount(tmp_path: Path) -> Iterator[_Mount]:
    chain = tls_chain(rotated_client_name="apchi-rotated")
    server_certificate = tmp_path / "server.pem"
    server_certificate.write_bytes(chain.server)
    server_key = tmp_path / "server-key.pem"
    server_key.write_bytes(chain.server_key)
    ca = tmp_path / "ca.pem"
    ca.write_bytes(chain.ca)
    certificate = tmp_path / "client.pem"
    certificate.write_bytes(chain.client)
    key = tmp_path / "client-key.pem"
    key.write_bytes(chain.client_key)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(server_certificate), str(server_key))
    context.load_verify_locations(cafile=str(ca))
    context.verify_mode = ssl.CERT_REQUIRED
    server = http.server.HTTPServer(("127.0.0.1", 0), _Identifier)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Mount(
            url=f"https://localhost:{server.server_address[1]}/",
            ca=str(ca),
            certificate=str(certificate),
            key=str(key),
            chain=chain,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _presented_over_requests(certificate: str | None, key: str | None, mount: _Mount) -> str:
    """What the Trino client's session shows, built the way the adapter builds it."""
    session = requests.Session()
    session.verify = bundled_ca(mount.ca)
    session.cert = (str(certificate), str(key))
    return session.get(mount.url).text


async def _presented_over_httpx(context: ssl.SSLContext | bool, mount: _Mount) -> str:
    """What the liveness check shows, from the context `verification` built."""
    async with httpx.AsyncClient(verify=context) as client:
        return (await client.get(mount.url)).text


async def test_the_mounted_paths_alone_would_diverge_on_a_rotation(mount: _Mount) -> None:
    """Not a test of Apchi but of the premise it is built on, like the trust-store counts
    above. `requests` is handed the two paths and reopens them at every handshake; httpx is
    handed a context with the bytes already in it. So the same rotation moves one and not the
    other, and this says so rather than leaving the copying in `presented` unjustified."""
    context = verification(mount.ca, mount.certificate, mount.key)
    assert _presented_over_requests(mount.certificate, mount.key, mount) == "apchi"
    assert await _presented_over_httpx(context, mount) == "apchi"

    mount.rotate()

    assert _presented_over_requests(mount.certificate, mount.key, mount) == "apchi-rotated"
    assert await _presented_over_httpx(context, mount) == "apchi"


async def test_both_halves_of_one_destination_agree_across_a_rotation(mount: _Mount) -> None:
    """The regression #193 is about. One coordinator reached through two libraries must not
    present two identities, so the adapter hands both of them copies `app.trust` owns.

    The certificate that wins is the one loaded at startup, deliberately: rotating needs a
    pod restart, which is what keeps MongoDB -- whose context cannot be rebuilt without
    rebuilding the client -- from being left behind trusting an older CA.
    """
    adapter = Trino(
        url=mount.url.rstrip("/"),
        ca_file=mount.ca,
        auth_certificate_file=mount.certificate,
        auth_key_file=mount.key,
    )
    assert (
        _presented_over_requests(adapter._auth_certificate_file, adapter._auth_key_file, mount)
        == "apchi"
    )
    assert await _presented_over_httpx(adapter._verification, mount) == "apchi"

    mount.rotate()

    assert (
        _presented_over_requests(adapter._auth_certificate_file, adapter._auth_key_file, mount)
        == "apchi"
    )
    assert await _presented_over_httpx(adapter._verification, mount) == "apchi"


def test_the_presented_certificate_is_a_copy_apchi_owns(mount: _Mount) -> None:
    """0600, which the mount cannot be: the kubelet ORs 0440 into every file's mode in a
    readOnly volume, so 0640 is the floor there (§13.2). A copy Apchi writes itself is the
    one place the key can be as closed as it should be."""
    certificate, key = presented(mount.certificate, mount.key)

    assert certificate is not None and key is not None
    assert (certificate, key) != (mount.certificate, mount.key)
    assert Path(certificate).read_bytes() == Path(mount.certificate).read_bytes()
    assert Path(key).read_bytes() == Path(mount.key).read_bytes()
    assert Path(certificate).stat().st_mode & 0o777 == 0o600
    assert Path(key).stat().st_mode & 0o777 == 0o600


def test_nothing_is_copied_when_no_certificate_is_configured() -> None:
    """The default shape. Plaintext Apchi writes no files and reads none."""
    assert presented(None, None) == (None, None)


def test_a_rotation_is_reported_and_not_refused(
    mount: _Mount, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Reported, because a certificate Apchi has not reread is still a working certificate
    until the destination stops accepting it. Refusing the Apply would block work that would
    have succeeded, and would do it in the middle of a correct rotation."""
    monkeypatch.setattr(trust, "_loaded", {})
    presented.cache_clear()
    presented(mount.certificate, mount.key)
    assert trust.rotated() == []

    mount.rotate()

    assert sorted(trust.rotated()) == sorted([mount.certificate, mount.key])
    with caplog.at_level(logging.WARNING, logger="app.trust"):
        trust.log_rotations()
    assert "restart Apchi" in caplog.text
    assert mount.certificate in caplog.text


def test_a_vanished_file_counts_as_a_rotation(
    mount: _Mount, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Secret key removed, or a volume no longer mounted. Unreadable is a change: Apchi is
    still presenting something the deployment no longer describes."""
    monkeypatch.setattr(trust, "_loaded", {})
    presented.cache_clear()
    presented(mount.certificate, mount.key)
    Path(mount.certificate).unlink()

    assert mount.certificate in trust.rotated()


def test_mongodbs_pem_is_remembered_without_being_copied(
    mount: _Mount, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PyMongo loads this into a context while validating its options, so it is already read
    exactly once and a copy would buy nothing -- while costing the error message that names
    the path the chart assembled."""
    monkeypatch.setattr(trust, "_loaded", {})
    combined = Path(mount.certificate).with_name("mongo.pem")
    combined.write_bytes(Path(mount.certificate).read_bytes() + Path(mount.key).read_bytes())
    settings = Settings(
        environment=Environment.TEST,
        ca_file=mount.ca,
        mongo_certificate_key_file=str(combined),
    )

    assert _tls_options(settings)["tlsCertificateKeyFile"] == str(combined)
    assert trust.rotated() == []

    combined.write_bytes(b"rotated")
    assert trust.rotated() == [str(combined)]


def test_a_missing_mongodb_pem_is_left_to_pymongo(monkeypatch: pytest.MonkeyPatch) -> None:
    """`recorded` must not raise first. PyMongo's own failure names the setting that put the
    path there, and an error from here would only arrive before it and say less."""
    monkeypatch.setattr(trust, "_loaded", {})

    assert trust.recorded("/nonexistent/mongo.pem") == "/nonexistent/mongo.pem"
    assert trust.rotated() == []


def test_the_expiry_of_a_presented_certificate_is_logged(
    mount: _Mount, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """On a deployment that rotates by restarting, the restart is the only moment an Admin
    can be told what was loaded and when it dies."""
    monkeypatch.setattr(trust, "_loaded", {})
    presented.cache_clear()
    with caplog.at_level(logging.INFO, logger="app.trust"):
        presented(mount.certificate, mount.key)

    expiry = x509.load_pem_x509_certificate(mount.chain.client).not_valid_after_utc
    assert "CN=apchi" in caplog.text
    assert expiry.isoformat() in caplog.text
    # The key file holds no certificate, so it says nothing rather than warning.
    assert "could not parse" not in caplog.text


async def test_an_apply_says_what_is_wrong_with_the_certificates_on_disk(
    applying_client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both wirings, through a real Apply rather than by calling either function directly.

    Before every Apply and not once at startup, for the same reason §16's preconditions are:
    a Secret can be rewritten under a long-lived pod at any moment, and the Apply is the
    operation whose failure would otherwise be attributed to the Cluster.

    One Apply for both because an Apply here costs a real Trino; the two calls are removed
    one at a time to check this fails for each.
    """
    mounted = tmp_path / "mounted.pem"
    mounted.write_bytes(b"what Apchi read")
    monkeypatch.setattr(trust, "_loaded", {})
    trust.recorded(str(mounted))
    mounted.write_bytes(b"what the kubelet put there since")
    expiring = _loads(tmp_path / "expiring.pem", valid_for_days=5)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.trust"):
        apply_id = (await applying_client.post("/api/v1/applies")).json()["id"]
        deadline = asyncio.get_running_loop().time() + 30.0
        while asyncio.get_running_loop().time() < deadline:
            record = (await applying_client.get(f"/api/v1/applies/{apply_id}")).json()
            if record["stage"] in TERMINAL:
                break
            await asyncio.sleep(0.02)

    # Warned, and the Apply still ran: what Apchi loaded works until the destination stops
    # accepting it, so refusing here would block an Apply that would have succeeded.
    assert record["stage"] == "succeeded"
    assert str(mounted) in caplog.text
    assert "restart Apchi" in caplog.text
    # And the expiry, on the same pass: the default window is 30 days.
    assert expiring in caplog.text
    assert "expires on" in caplog.text


# --------------------------------------------------------------------------------------
# Expiry. Logged where Apchi is already doing something, because there is no timer to log
# it on and this is not the feature to add the first one for (#196).
# --------------------------------------------------------------------------------------


def _loads(path: Path, valid_for_days: int, common_name: str = "apchi") -> str:
    """A certificate `valid_for_days` from expiry, read the way Apchi reads one.

    `key_pair` rather than `tls_chain`: nothing here performs a handshake, so the extensions
    a real chain needs would only cost two RSA key generations per case.

    Registering it logs the "loaded certificate" line, which is why every caller clears
    `caplog` before the call under test -- otherwise the setup is part of the assertion.
    """
    certificate, _ = key_pair(common_name, valid_for_days)
    path.write_bytes(certificate_pem(certificate))
    trust.recorded(str(path))
    return str(path)


def test_a_certificate_inside_the_window_is_named_with_its_remaining_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(trust, "_loaded", {})
    path = _loads(tmp_path / "soon.pem", valid_for_days=5)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.trust"):
        trust.log_expiring(30)

    assert path in caplog.text
    assert "in 4 days" in caplog.text or "in 5 days" in caplog.text


def test_a_certificate_outside_the_window_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Every Apply calls this, so a cluster whose certificates are fine must stay quiet --
    a warning on each Apply is one an Admin learns to scroll past."""
    monkeypatch.setattr(trust, "_loaded", {})
    _loads(tmp_path / "fine.pem", valid_for_days=365)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.trust"):
        trust.log_expiring(30)

    assert caplog.text == ""


def test_an_expired_certificate_is_reported_more_loudly_than_an_expiring_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Separable by level, so an alert rule can tell "renew this soon" from "this is already
    broken" without matching on message text."""
    monkeypatch.setattr(trust, "_loaded", {})
    expired = _loads(tmp_path / "gone.pem", valid_for_days=-1)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.trust"):
        trust.log_expiring(30)

    levels = {record.levelno for record in caplog.records}
    assert levels == {logging.ERROR}
    assert expired in caplog.text
    assert "expired" in caplog.text


def test_a_file_with_no_certificate_in_it_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A private key on its own. `presented` registers both halves, so this is the common
    case rather than an edge one."""
    monkeypatch.setattr(trust, "_loaded", {})
    key_only = tmp_path / "client-key.pem"
    key_only.write_bytes(key_pem(key_pair("apchi")[1]))
    trust.recorded(str(key_only))

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.trust"):
        trust.log_expiring(30)

    assert caplog.text == ""


def test_the_expiry_reported_is_the_one_apchi_loaded_not_the_one_on_the_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The #193 trap, one layer up. Apchi presents the copy it made at startup, so reading
    the mount to answer "am I about to expire?" would describe a replacement Apchi is not
    using -- and would fall silent exactly when a rotation had been staged but not applied by
    a restart."""
    monkeypatch.setattr(trust, "_loaded", {})
    mounted = tmp_path / "client.pem"
    _loads(mounted, valid_for_days=5)
    # The kubelet projects the renewal. Apchi has not restarted, so it still presents the
    # certificate that is about to expire.
    replacement, _ = key_pair("apchi", valid_for_days=365)
    mounted.write_bytes(certificate_pem(replacement))

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.trust"):
        trust.log_expiring(30)

    assert "expires" in caplog.text
    assert str(mounted) in caplog.text

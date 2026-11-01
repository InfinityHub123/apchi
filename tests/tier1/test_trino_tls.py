"""Reaching a coordinator that serves HTTPS, and authenticating to it (#188).

Against a real TLS server rather than a mock, because what broke here was not logic: the
Trino client reads `https` from a port only when the port is **exactly 443**, so Trino's
documented 8443 came out as plaintext and nothing said so. A test that asserted on a
constructed URL would have passed while the handshake never happened.

The server demands a client certificate, which is what a coordinator with
`http-server.authentication.type=CERTIFICATE` does.
"""

import http.server
import json
import ssl
import threading
from collections.abc import Iterator

import httpx
import pytest
import trino.auth
import trino.dbapi
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.adapters.trino import Trino
from tests.certificates import TlsChain, tls_chain
from tests.conftest import TRINO_IMAGE

#: A real Trino that serves HTTPS and authenticates its callers by certificate -- the
#: deployment §13.3 calls production, and the one #188 was filed about.
#:
#: `allow-insecure-over-http` is on so that the coordinator's own discovery still works over
#: http in a single container. What is under test is Apchi's client against Trino's TLS
#: listener and its certificate authenticator, not Trino's closing of the plaintext port --
#: that behaviour is documented and is what precondition 9 reads config.properties for.
_TLS_CONFIG = """coordinator=true
node-scheduler.include-coordinator=true
http-server.http.port=8080
http-server.https.enabled=true
http-server.https.port=8443
http-server.https.keystore.path=/etc/trino/tls/server.pem
http-server.https.truststore.path=/etc/trino/tls/ca.pem
http-server.authentication.type=CERTIFICATE
http-server.authentication.allow-insecure-over-http=true
http-server.authentication.certificate.user-mapping.pattern=CN=(.*)
internal-communication.shared-secret=an-arbitrary-shared-secret-for-this-test
discovery.uri=http://localhost:8080
"""


class _Info(http.server.BaseHTTPRequestHandler):
    """Just enough of `/v1/info` to answer `is_starting`, and who asked."""

    def do_GET(self) -> None:
        peer = self.connection.getpeercert()  # type: ignore[attr-defined]
        common_name = dict(attribute[0] for attribute in peer["subject"])["commonName"]
        body = json.dumps({"starting": False, "peerCommonName": common_name}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silent: the failing cases here are meant to fail, and their tracebacks are noise."""


@pytest.fixture(scope="module")
def chain() -> TlsChain:
    return tls_chain()


@pytest.fixture(scope="module")
def pem_files(chain: TlsChain, tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    directory = tmp_path_factory.mktemp("trino-tls")
    written = {}
    for name, content in (
        ("ca", chain.ca),
        ("server", chain.server),
        ("server_key", chain.server_key),
        ("client", chain.client),
        ("client_key", chain.client_key),
    ):
        path = directory / f"{name}.pem"
        path.write_bytes(content)
        written[name] = str(path)
    return written


@pytest.fixture(scope="module")
def coordinator(pem_files: dict[str, str]) -> Iterator[str]:
    """An HTTPS listener on an arbitrary port that requires a client certificate.

    Port 0 deliberately: a fixed one would collide with whatever else tier 1 is running,
    and the point of #188 is that the port must not decide the scheme anyway.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pem_files["server"], pem_files["server_key"])
    context.load_verify_locations(pem_files["ca"])
    context.verify_mode = ssl.CERT_REQUIRED

    server = http.server.HTTPServer(("127.0.0.1", 0), _Info)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_a_coordinator_on_https_is_reachable_with_the_ca_and_a_certificate(
    coordinator: str, pem_files: dict[str, str]
) -> None:
    trino = Trino(
        url=coordinator,
        ca_file=pem_files["ca"],
        auth_certificate_file=pem_files["client"],
        auth_key_file=pem_files["client_key"],
    )
    assert await trino.is_starting() is False


async def test_the_coordinator_sees_the_certificate_apchi_was_given(
    coordinator: str, pem_files: dict[str, str]
) -> None:
    """`is_starting` returning an answer is not proof that the certificate was presented --
    a server that merely allowed one would look identical. This asks the server who it saw."""
    trino = Trino(
        url=coordinator,
        ca_file=pem_files["ca"],
        auth_certificate_file=pem_files["client"],
        auth_key_file=pem_files["client_key"],
    )
    async with httpx.AsyncClient(verify=trino._verification) as client:
        response = await client.get(f"{coordinator}/v1/info")
    assert response.json()["peerCommonName"] == "apchi"


async def test_without_a_certificate_the_coordinator_refuses(
    coordinator: str, pem_files: dict[str, str]
) -> None:
    """None, not an exception: `is_starting` reports every failure as "cannot be reached",
    which is why precondition 9 exists to name this case before an Apply starts."""
    trino = Trino(url=coordinator, ca_file=pem_files["ca"])
    assert await trino.is_starting() is None


async def test_without_the_ca_the_coordinators_certificate_is_not_trusted(
    coordinator: str, pem_files: dict[str, str]
) -> None:
    trino = Trino(
        url=coordinator,
        auth_certificate_file=pem_files["client"],
        auth_key_file=pem_files["client_key"],
    )
    assert await trino.is_starting() is None


async def test_plaintext_against_a_tls_listener_is_unreachable(coordinator: str) -> None:
    """The whole of #188 in one case. This is what `trino_port: 8443` used to produce."""
    trino = Trino(url=coordinator.replace("https://", "http://"))
    assert await trino.is_starting() is None


def test_the_url_decides_the_scheme_and_not_the_port() -> None:
    """The client's own inference reads https from a port only at exactly 443, so 8443 --
    the port Trino's documentation uses -- resolved to http. Passing the URL as its `host`
    takes precedence over that, which is why the setting is a URL."""
    for url, expected in (
        ("https://trino:8443", "https"),
        ("http://trino:8080", "http"),
        ("https://trino", "https"),
    ):
        connection = trino.dbapi.connect(host=Trino(url=url)._base, user="apchi")
        assert connection.http_scheme == expected, url


def test_the_query_session_carries_the_ca_and_the_certificate(
    pem_files: dict[str, str],
) -> None:
    """Queries go through the Trino client's `requests` session, not httpx, and `requests`
    ignores SSL_CERT_FILE -- it reads REQUESTS_CA_BUNDLE. So both are passed explicitly
    rather than left to the environment, and this asserts they arrive."""
    trino_adapter = Trino(
        url="https://trino:8443",
        ca_file=pem_files["ca"],
        auth_certificate_file=pem_files["client"],
        auth_key_file=pem_files["client_key"],
    )
    connection = trino.dbapi.connect(
        host=trino_adapter._base,
        user="apchi",
        verify=trino_adapter._ca_file,
        auth=trino.auth.CertificateAuthentication(pem_files["client"], pem_files["client_key"]),
    )
    session = connection._create_request()._http_session
    assert session.verify == pem_files["ca"]
    assert session.cert == (pem_files["client"], pem_files["client_key"])


def test_a_certificate_without_its_key_is_refused_at_startup(pem_files: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="go together"):
        Trino(url="https://trino:8443", auth_certificate_file=pem_files["client"])


def test_a_certificate_over_plaintext_is_refused_at_startup(pem_files: dict[str, str]) -> None:
    """Trino requires TLS under every authentication type it has, so this combination is
    wrong from the first second rather than at the first query."""
    with pytest.raises(ValueError, match="requires TLS"):
        Trino(
            url="http://trino:8080",
            auth_certificate_file=pem_files["client"],
            auth_key_file=pem_files["client_key"],
        )


def test_a_missing_pem_stops_apchi_rather_than_reporting_a_down_cluster() -> None:
    """`is_starting` swallows every exception, so a path typo inside it would read as a
    coordinator that is down. The context is built in the constructor to keep that
    distinguishable."""
    with pytest.raises(FileNotFoundError):
        Trino(url="https://trino:8443", ca_file="/nonexistent/ca.pem")


def test_plaintext_needs_no_tls_configuration_at_all() -> None:
    """The default shape, which every other tier 1 test uses: no context, no files read."""
    assert Trino(url="http://trino:8080")._verification is True


@pytest.fixture(scope="module")
def tls_trino(chain: TlsChain, pem_files: dict[str, str]) -> Iterator[Trino]:
    """Trino itself, serving HTTPS and demanding a certificate.

    A separate container from the suite's other two because its `config.properties` is
    replaced wholesale, and the point is to prove Apchi's client against **Trino's** TLS
    stack rather than against a Python one. Rendering a chart is not installing it, and a
    handshake with `http.server` above is not a handshake with Trino.

    The server's files are **copied in** rather than bind-mounted. A bind mount needs the
    container's uid to traverse the host directory, and pytest's temporary directories are
    0700 and owned by whoever runs the suite -- which works only where that uid happens to
    equal the image's. It did locally and did not in CI, where the four tests below were the
    whole of the failure. `with_copy_into_container` takes the bytes and lands them before
    the container's process starts, so no host directory is involved at all. Apchi's own
    certificate still comes from `pem_files`, because that one is read by this process.
    """
    container = (
        DockerContainer(TRINO_IMAGE)
        .with_exposed_ports(8443)
        .with_copy_into_container(_TLS_CONFIG.encode(), "/etc/trino/config.properties")
        .with_copy_into_container(chain.ca, "/etc/trino/tls/ca.pem")
        # Trino's keystore.path takes one PEM holding the certificate and its key together.
        .with_copy_into_container(chain.server + chain.server_key, "/etc/trino/tls/server.pem")
        .waiting_for(LogMessageWaitStrategy("SERVER STARTED"))
    )
    with container as started:
        host = started.get_container_host_ip()
        if host not in ("localhost", "127.0.0.1"):
            # Said out loud rather than papered over. The server certificate's SAN names
            # localhost and 127.0.0.1, and matching the SAN is the whole point of giving
            # Apchi the CA -- so against a remote Docker daemon this would fail hostname
            # verification and read as a bug in the adapter.
            pytest.skip(f"the test certificate names localhost, and Docker is at {host}")
        yield Trino(
            url=f"https://localhost:{started.get_exposed_port(8443)}",
            user="apchi",
            ca_file=pem_files["ca"],
            auth_certificate_file=pem_files["client"],
            auth_key_file=pem_files["client_key"],
        )


async def test_a_real_trino_on_https_answers_apchi(tls_trino: Trino) -> None:
    assert await tls_trino.is_starting() is False


async def test_trino_resolves_apchis_identity_from_the_certificate(tls_trino: Trino) -> None:
    """The one assertion that proves authentication rather than encryption. Trino maps the
    certificate's subject to a Trino Identity through
    `http-server.authentication.certificate.user-mapping.pattern`, and `current_user` is
    what it decided -- so a connection that was merely encrypted would not reach here."""
    rows = await tls_trino.query("SELECT current_user")
    assert rows[0][0] == "apchi"


async def test_the_adapters_own_queries_work_over_certificate_authentication(
    tls_trino: Trino,
) -> None:
    """Not just any query: the two the pipeline depends on. `catalogs()` backs every
    Verification and `system.runtime.nodes` is how cluster membership is read."""
    assert "system" in await tls_trino.catalogs()
    assert await tls_trino.active_worker_count() == 0


async def test_a_real_trino_refuses_apchi_without_the_certificate(tls_trino: Trino) -> None:
    """401, not a TLS error: the handshake succeeds and Trino then declines. This is why
    precondition 9 checks for the certificate separately from the scheme."""
    import trino.exceptions

    unauthenticated = Trino(url=tls_trino._base, user="apchi", ca_file=tls_trino._ca_file)
    with pytest.raises(trino.exceptions.HttpError, match="401"):
        await unauthenticated.query("SELECT 1")

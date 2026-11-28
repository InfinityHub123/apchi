"""Reaching a coordinator that serves HTTPS (#188).

Against a real TLS server rather than a mock, because what broke here was not logic: the
Trino client reads `https` from a port only when the port is **exactly 443**, so Trino's
documented 8443 came out as plaintext and nothing said so. A test that asserted on a
constructed URL would have passed while the handshake never happened.

Apchi presents no certificate: it uses the coordinator's HTTP port inside the namespace,
which takes a user name (#295). So what is left of HTTPS here is the URL and the CA.
"""

import http.server
import json
import ssl
import threading
from collections.abc import Iterator

import pytest
import trino.dbapi

from app.adapters.trino import Trino
from app.trust import bundled_ca
from tests.certificates import TlsChain, tls_chain


class _Info(http.server.BaseHTTPRequestHandler):
    """Just enough of `/v1/info` to answer `is_starting`."""

    def do_GET(self) -> None:
        body = json.dumps({"starting": False}).encode()
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
    ):
        path = directory / f"{name}.pem"
        path.write_bytes(content)
        written[name] = str(path)
    return written


@pytest.fixture(scope="module")
def coordinator(pem_files: dict[str, str]) -> Iterator[str]:
    """An HTTPS listener on an arbitrary port.

    Port 0 deliberately: a fixed one would collide with whatever else tier 1 is running,
    and the point of #188 is that the port must not decide the scheme anyway.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pem_files["server"], pem_files["server_key"])

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


async def test_a_coordinator_on_https_is_reachable_with_the_ca(
    coordinator: str, pem_files: dict[str, str]
) -> None:
    trino = Trino(url=coordinator, ca_file=pem_files["ca"])
    assert await trino.is_starting() is False


async def test_without_the_ca_the_coordinators_certificate_is_not_trusted(
    coordinator: str,
) -> None:
    trino = Trino(url=coordinator)
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


def test_the_query_session_carries_the_ca(pem_files: dict[str, str]) -> None:
    """Queries go through the Trino client's `requests` session, not httpx, and `requests`
    ignores SSL_CERT_FILE -- it reads REQUESTS_CA_BUNDLE. So the CA is passed explicitly
    rather than left to the environment, and this asserts it arrives. The CA that arrives is
    the *concatenated* bundle, not the configured file: `requests` replaces its trust store
    with whatever path it is given, so handing it the raw CA would make queries distrust
    everything the liveness check still trusted (#187)."""
    trino_adapter = Trino(url="https://trino:8443", ca_file=pem_files["ca"])
    connection = trino.dbapi.connect(
        host=trino_adapter._base, user="apchi", verify=trino_adapter._ca_bundle
    )
    session = connection._create_request()._http_session
    assert session.verify == bundled_ca(pem_files["ca"])


def test_a_missing_pem_stops_apchi_rather_than_reporting_a_down_cluster() -> None:
    """`is_starting` swallows every exception, so a path typo inside it would read as a
    coordinator that is down. Both the bundle and the context are built in the constructor to
    keep that distinguishable.

    A `RuntimeError` naming `ca_file` rather than a bare FileNotFoundError, because the CA is
    shared with every other destination now and `app.trust` is the one place that can say so
    (#187)."""
    with pytest.raises(RuntimeError, match="ca_file"):
        Trino(url="https://trino:8443", ca_file="/nonexistent/ca.pem")


def test_plaintext_needs_no_tls_configuration_at_all() -> None:
    """The default shape, which every other tier 1 test uses: no context, no files read."""
    assert Trino(url="http://trino:8080")._verification is True

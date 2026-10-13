"""The authorization code flow, completed against a real identity provider.

Tier 1 proves the refusals, with a provider that can be asked to misbehave (`tests/oidc.py`).
This proves the thing a fake cannot: that the flow Apchi implements is the flow a real OIDC
provider expects. A flow proved only against something Apchi's own author wrote is a flow
nobody has proved -- the fake would happily agree with the same mistake twice.

**Dex** is the provider, for reasons that are about testing rather than preference: it is one
Go binary that starts in about a second, it does real RS256 over a real JWKS, it advertises
S256 PKCE, and its `mockCallback` connector authenticates a fixed person with no login form at
all. That last part is what makes this runnable without a browser: Dex's own redirects carry
the flow from Apchi's `/auth` redirect to Apchi's callback in three hops, and the test plays
only the part a browser plays -- carrying cookies and following `Location`.

What varies between the two tests here is the *mapping*, not the provider: Dex's mock identity
is in the group `authors`, so pointing `APCHI_OIDC_ADMIN_GROUPS` at it makes that person an
Admin and pointing it elsewhere makes them an Operator. The claim mapping is what is under
test; which groups a provider happens to hold is not.
"""

import time
import urllib.parse
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.adapters.trino import Trino
from app.config import AuthMode, Settings
from app.main import create_app
from tests.tier2.conftest import ForwardedKubernetes, PortForward, free_port

pytestmark = pytest.mark.tier2

DEX_IMAGE = "ghcr.io/dexidp/dex:v2.44.0"
CLIENT_ID = "apchi"
CLIENT_SECRET = "the-client-secret"
REDIRECT_URL = "http://apchi/api/v1/auth/sso/callback"
#: Dex's mockCallback connector authenticates this person, in this group, every time.
MOCK_EMAIL = "kilgore@kilgore.trout"
MOCK_GROUP = "authors"

_CONFIG = """
issuer: http://127.0.0.1:{port}/dex
storage:
  type: memory
web:
  http: 0.0.0.0:5556
oauth2:
  skipApprovalScreen: true
staticClients:
  - id: {client_id}
    name: Apchi
    secret: {client_secret}
    redirectURIs:
      - {redirect_url}
enablePasswordDB: false
connectors:
  - type: mockCallback
    id: mock
    name: Mock
"""


@pytest.fixture(scope="module")
def dex(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A real OIDC provider, returning its issuer.

    The port is chosen before the container starts and bound rather than mapped, because an
    OIDC issuer is part of its own identity: it appears in the discovery document and in every
    token's `iss`, and Apchi checks both against what it was configured with. A port Dex
    learned about only after writing its configuration could not be in it.
    """
    port = free_port()
    issuer = f"http://127.0.0.1:{port}/dex"
    config = tmp_path_factory.mktemp("dex") / "config.yaml"
    config.write_text(
        _CONFIG.format(
            port=port,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_url=REDIRECT_URL,
        )
    )
    container = (
        DockerContainer(DEX_IMAGE)
        .with_bind_ports(5556, port)
        .with_volume_mapping(str(config), "/etc/dex/config.yaml", "ro")
        .with_command("dex serve /etc/dex/config.yaml")
        .waiting_for(LogMessageWaitStrategy("listening on"))
    )
    with container:
        _await_discovery(issuer)
        yield issuer


def _await_discovery(issuer: str, timeout: float = 30.0) -> None:
    """Serving the port is not the same as serving discovery, and a login that raced the
    provider's startup would be a flake nobody could reproduce."""
    deadline = time.monotonic() + timeout
    last: str = "it never answered"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=5)
        except httpx.HTTPError as unreachable:
            last = str(unreachable)
        else:
            if response.status_code == 200 and response.json().get("issuer") == issuer:
                return
            last = f"HTTP {response.status_code}"
        time.sleep(0.5)
    raise AssertionError(f"dex never served discovery at {issuer}: {last}")


def _sso_settings(settings: Settings, admin_groups: str) -> Settings:
    return settings.model_copy(
        update={
            "auth_mode": AuthMode.SSO,
            "oidc_client_id": CLIENT_ID,
            "oidc_client_secret": CLIENT_SECRET,
            "oidc_redirect_url": REDIRECT_URL,
            # Dex's `sub` is an opaque blob and its mock connector sends no
            # preferred_username, so the email is the readable name here -- which is exactly
            # the case APCHI_OIDC_USERNAME_CLAIM exists for.
            "oidc_username_claim": "email",
            "oidc_admin_groups": admin_groups,
        }
    )


async def _sign_in(http: AsyncClient) -> dict[str, object]:
    """Play the browser: follow Apchi out to Dex, follow Dex back, and finish at Apchi.

    Dex's redirects are followed with a separate client because Apchi is in process behind an
    ASGI transport and has no socket for Dex to redirect a real browser to. The cookies that
    matter stay on `http`, which is the client Apchi sees.
    """
    started = await http.get("/api/v1/auth/sso/login")
    assert started.status_code == 307, started.text

    async with httpx.AsyncClient(follow_redirects=False, timeout=15) as browser:
        at = started.headers["location"]
        for _ in range(8):
            hop = await browser.get(at)
            location = hop.headers.get("location")
            assert location is not None, f"dex stopped redirecting at {at}: {hop.status_code}"
            at = urllib.parse.urljoin(at, location)
            if at.startswith("http://apchi/"):
                break
        else:
            raise AssertionError("dex never redirected back to Apchi")

    query = urllib.parse.parse_qs(urllib.parse.urlparse(at).query)
    assert "code" in query, f"dex sent no code: {query}"
    finished = await http.get(
        "/api/v1/auth/sso/callback",
        params={"code": query["code"][0], "state": query["state"][0]},
    )
    assert finished.status_code == 200, finished.text
    identity: dict[str, object] = finished.json()
    return identity


@pytest.fixture
async def sso_apchi(
    settings: Settings,
    forwarded_kubernetes: ForwardedKubernetes,
    forward: PortForward,
    dex: str,
    request: pytest.FixtureRequest,
) -> AsyncIterator[AsyncClient]:
    """Apchi in `sso` mode against Dex. Parametrised by which group means Admin."""
    admin_groups = getattr(request, "param", "")
    configured = _sso_settings(settings, admin_groups).model_copy(update={"oidc_issuer": dex})
    app = create_app(configured)
    app.state.kubernetes = forwarded_kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=forward.port, user=configured.trino_user)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


@pytest.mark.parametrize("sso_apchi", [MOCK_GROUP], indirect=True)
async def test_the_flow_completes_against_a_real_provider(sso_apchi: AsyncClient) -> None:
    """The whole point of tier 2 for this slice: a real authorization code exchange, with
    real PKCE, and an ID token verified against the provider's published JWKS."""
    identity = await _sign_in(sso_apchi)

    assert identity["subject"] == MOCK_EMAIL
    assert identity["via"] == "sso"
    assert identity["role"] == "admin", "the mock identity is in the group named as Admin"


@pytest.mark.parametrize("sso_apchi", [MOCK_GROUP], indirect=True)
async def test_the_session_reaches_the_operator_and_admin_surfaces(
    sso_apchi: AsyncClient,
) -> None:
    """A session established by a real provider is a session the rest of the API accepts.
    Proved on a real route rather than on /auth/me, which would only prove the cookie."""
    await _sign_in(sso_apchi)

    assert (await sso_apchi.get("/api/v1/catalogs")).status_code == 200
    assert (await sso_apchi.get("/api/v1/admin/maintenance-mode")).status_code == 200


@pytest.mark.parametrize("sso_apchi", ["a-group-nobody-is-in"], indirect=True)
async def test_a_subject_in_no_admin_group_is_an_operator(sso_apchi: AsyncClient) -> None:
    """Same provider, same person, a different mapping. Operator is the deliberate default:
    a mapping that matches nothing must leave the Sections working and /admin/* closed."""
    identity = await _sign_in(sso_apchi)

    assert identity["role"] == "operator"
    assert (await sso_apchi.get("/api/v1/catalogs")).status_code == 200
    assert (await sso_apchi.get("/api/v1/admin/maintenance-mode")).status_code == 403


@pytest.mark.parametrize("sso_apchi", [MOCK_GROUP], indirect=True)
async def test_a_replayed_callback_is_refused_by_a_real_flow(sso_apchi: AsyncClient) -> None:
    """Dex's authorization codes are single use, and so is Apchi's pending login. Either one
    refusing is enough; this asserts that between them the second attempt never signs in."""
    started = await sso_apchi.get("/api/v1/auth/sso/login")
    async with httpx.AsyncClient(follow_redirects=False, timeout=15) as browser:
        at = started.headers["location"]
        for _ in range(8):
            hop = await browser.get(at)
            at = urllib.parse.urljoin(at, hop.headers["location"])
            if at.startswith("http://apchi/"):
                break
    query = urllib.parse.parse_qs(urllib.parse.urlparse(at).query)
    params = {"code": query["code"][0], "state": query["state"][0]}

    first = await sso_apchi.get("/api/v1/auth/sso/callback", params=params)
    replayed = await sso_apchi.get("/api/v1/auth/sso/callback", params=params)

    assert first.status_code == 200, first.text
    assert replayed.status_code == 401, replayed.text


@pytest.mark.parametrize("sso_apchi", [MOCK_GROUP], indirect=True)
async def test_a_token_minted_after_an_sso_login_works(sso_apchi: AsyncClient) -> None:
    """The two slices meeting: SSO mirrors the person into the same collection tokens resolve
    an owner from, so an automation minted here answers to that person's role (#131)."""
    await _sign_in(sso_apchi)
    minted = await sso_apchi.post("/api/v1/tokens", json={"name": "nightly"})
    assert minted.status_code == 201, minted.text

    async with AsyncClient(
        transport=ASGITransport(app=sso_apchi._transport.app),  # type: ignore[attr-defined]
        base_url="http://apchi",
        headers={"authorization": f"Bearer {minted.json()['secret']}"},
    ) as automation:
        response = await automation.get("/api/v1/catalogs")

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("sso_apchi", [MOCK_GROUP], indirect=True)
async def test_the_client_secret_never_reaches_the_log(
    sso_apchi: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Against a real provider, where the secret really is sent. Asserted rather than
    assumed, which is why the redaction filter is unconditional."""
    with caplog.at_level("DEBUG"):
        await _sign_in(sso_apchi)

    assert CLIENT_SECRET not in caplog.text

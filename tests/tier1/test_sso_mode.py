"""Signing in through an identity provider, and every way that has to fail.

Three properties carry this slice, and none of them is "a correct login works":

* **A token is only worth what its signature is worth.** Unverified, `groups` is whatever the
  browser chose to send, which would make Admin a claim rather than a fact. So a token signed
  by a key the provider never published is refused, and the test proves it by signing one.
* **A callback can be replayed.** The `state` and the code travel through a browser, through
  the provider's logs, and through anything in between. Apchi refuses the second use of
  either, and it does so because `take` deleted the attempt -- storage, not vigilance.
* **Nothing degrades.** A provider that is down makes login fail. It never falls back to a
  mode that lets somebody in, which is the failure a control plane cannot afford to have.

The provider here is a fake in what it decides and real in what it computes (`tests/oidc.py`).
The flow against a real provider is tier 2's job.
"""

import urllib.parse
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.auth.oidc import OidcProvider
from app.auth.passwords import hash_password
from app.auth.users import User
from app.config import AuthMode, Settings
from app.main import create_app
from tests.conftest import FakeKubernetes
from tests.oidc import CLIENT_ID, CLIENT_SECRET, ISSUER, REDIRECT_URL, StubProvider

START = "/api/v1/auth/sso/login"
CALLBACK = "/api/v1/auth/sso/callback"
ME = "/api/v1/auth/me"
LOGIN = "/api/v1/auth/login"
REFUSED = "That SSO login could not be completed."


def _settings(settings: Settings, **overrides: object) -> Settings:
    return settings.model_copy(
        update={
            "auth_mode": AuthMode.SSO,
            "oidc_issuer": ISSUER,
            "oidc_client_id": CLIENT_ID,
            "oidc_client_secret": CLIENT_SECRET,
            "oidc_redirect_url": REDIRECT_URL,
            "oidc_admin_groups": "platform-admins",
            **overrides,
        }
    )


async def _serving(app: FastAPI, fake_kubernetes: FakeKubernetes) -> AsyncIterator[AsyncClient]:
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=1)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


@pytest.fixture
def provider() -> StubProvider:
    return StubProvider()


@pytest.fixture
async def sso(
    settings: Settings, fake_kubernetes: FakeKubernetes, provider: StubProvider
) -> AsyncIterator[AsyncClient]:
    """An Apchi in `sso` mode whose provider is the stub, reached over an in-process client."""
    app = create_app(_settings(settings))
    app.state.oidc = OidcProvider(_settings(settings), http=provider.client())
    async for http in _serving(app, fake_kubernetes):
        yield http


async def _authorize(http: AsyncClient, provider: StubProvider) -> tuple[str, str]:
    """Walk the first half of the flow, returning the code and state the provider sends back.

    The redirect from Apchi goes to the stub, and the stub's redirect comes back to Apchi. The
    test plays the browser in between because there is no browser: what it carries is the
    login cookie, which `http` keeps on its own.
    """
    started = await http.get(START)
    assert started.status_code == 307, started.text
    async with provider.client() as at_provider:
        handed_back = await at_provider.get(started.headers["location"])
    query = urllib.parse.parse_qs(urllib.parse.urlparse(handed_back.headers["location"]).query)
    return query["code"][0], query["state"][0]


async def _sign_in(http: AsyncClient, provider: StubProvider) -> AsyncClient:
    code, state = await _authorize(http, provider)
    response = await http.get(CALLBACK, params={"code": code, "state": state})
    assert response.status_code == 200, response.text
    return http


# --- a login that works -----------------------------------------------------------------


async def test_a_login_signs_in_and_says_who_you_are(
    sso: AsyncClient, provider: StubProvider
) -> None:
    code, state = await _authorize(sso, provider)

    response = await sso.get(CALLBACK, params={"code": code, "state": state})

    assert response.status_code == 200, response.text
    assert response.json() == {"subject": "jo", "role": "operator", "via": "sso", "token_id": None}


async def test_the_session_carries_the_identity_afterwards(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """The cookie is the credential every later request uses, so the proof is a second
    request rather than the callback's own response."""
    await _sign_in(sso, provider)

    response = await sso.get(ME)

    assert response.status_code == 200, response.text
    assert response.json()["subject"] == "jo"
    assert response.json()["via"] == "sso", "§20's audit has to tell an SSO login from a password"


async def test_the_authorization_url_carries_pkce_and_a_nonce(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """Asserted on the URL rather than inferred from the login working: a flow that omitted
    the challenge would still complete against a provider that does not insist on it."""
    started = await sso.get(START)

    query = urllib.parse.parse_qs(urllib.parse.urlparse(started.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    assert query["nonce"][0]
    assert query["redirect_uri"] == [REDIRECT_URL]


async def test_the_login_cookie_is_http_only(sso: AsyncClient) -> None:
    """It holds the handle to the attempt's state, nonce and PKCE verifier. A page's
    JavaScript reading it is an injected script reading it."""
    started = await sso.get(START)

    assert "httponly" in started.headers["set-cookie"].lower()


# --- roles come from a claim ------------------------------------------------------------


async def test_a_member_of_an_admin_group_is_an_admin(
    sso: AsyncClient, provider: StubProvider
) -> None:
    provider.claims["groups"] = ["everyone", "platform-admins"]

    await _sign_in(sso, provider)

    assert (await sso.get(ME)).json()["role"] == "admin"
    assert (await sso.get("/api/v1/admin/maintenance-mode")).status_code == 200


async def test_any_other_authenticated_subject_is_an_operator(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """The deliberate default. A claim mapping that matches nothing has to leave a Cluster
    whose six Sections work and whose /admin/* is closed -- not one where everybody is an
    Admin."""
    provider.claims["groups"] = ["some-other-team"]

    await _sign_in(sso, provider)

    assert (await sso.get(ME)).json()["role"] == "operator"
    assert (await sso.get("/api/v1/admin/maintenance-mode")).status_code == 403


async def test_a_missing_role_claim_is_an_operator_not_an_admin(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """A provider that sends no groups at all is the same case as one that sends the wrong
    ones, and the answer has to be the same: the lesser role."""
    provider.claims.pop("groups")

    await _sign_in(sso, provider)

    assert (await sso.get(ME)).json()["role"] == "operator"


async def test_a_group_change_takes_effect_at_the_next_login(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """The provider is authoritative for the role, re-read from the claim each login. As fresh
    as OIDC gets: nothing tells Apchi when a group changes."""
    await _sign_in(sso, provider)
    assert (await sso.get(ME)).json()["role"] == "operator"

    provider.claims["groups"] = ["platform-admins"]
    await _sign_in(sso, provider)

    assert (await sso.get(ME)).json()["role"] == "admin"


async def test_a_role_claim_that_is_a_bare_string_still_maps(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """Providers disagree about whether a single group is a string or a list of one, and one
    that sends "platform-admins" is not sending nothing."""
    provider.claims["groups"] = "platform-admins"

    await _sign_in(sso, provider)

    assert (await sso.get(ME)).json()["role"] == "admin"


# --- the refusals -----------------------------------------------------------------------


async def test_a_token_signed_by_the_wrong_key_is_refused(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """The one that matters most. Without the signature check every other claim in the token
    is whatever the sender chose, and `groups` would make Admin self-assigned."""
    provider.sign_with_an_unpublished_key = True
    code, state = await _authorize(sso, provider)

    response = await sso.get(CALLBACK, params={"code": code, "state": state})

    assert response.status_code == 401, response.text
    assert response.json()["message"] == REFUSED
    assert (await sso.get(ME)).status_code == 401, "a refused login must leave no session"


async def test_a_replayed_callback_is_refused(sso: AsyncClient, provider: StubProvider) -> None:
    """The same code and state, used twice. The second finds no attempt, because reading one
    deletes it."""
    code, state = await _authorize(sso, provider)
    first = await sso.get(CALLBACK, params={"code": code, "state": state})
    assert first.status_code == 200, first.text

    replayed = await sso.get(CALLBACK, params={"code": code, "state": state})

    assert replayed.status_code == 401, replayed.text


async def test_a_mismatched_state_is_refused(sso: AsyncClient, provider: StubProvider) -> None:
    """A callback carrying somebody else's state, with this browser's login cookie."""
    code, _ = await _authorize(sso, provider)

    response = await sso.get(CALLBACK, params={"code": code, "state": "not-the-state"})

    assert response.status_code == 401, response.text


async def test_a_callback_without_the_login_cookie_is_refused(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """What binds the callback to the user agent that started the flow (RFC 6749 §10.12): a
    state lifted from a provider's logs arrives without the cookie."""
    code, state = await _authorize(sso, provider)
    sso.cookies.clear()

    response = await sso.get(CALLBACK, params={"code": code, "state": state})

    assert response.status_code == 401, response.text


async def test_a_token_minted_for_another_login_is_refused(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """The nonce's whole job. A genuinely signed token from the right provider, for the right
    audience, that simply belongs to a different login attempt."""
    provider.nonce_to_echo = "a-nonce-from-somewhere-else"
    code, state = await _authorize(sso, provider)

    response = await sso.get(CALLBACK, params={"code": code, "state": state})

    assert response.status_code == 401, response.text


async def test_a_provider_that_refuses_is_reported_as_a_failed_login(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """The provider's own refusal -- consent declined, an unknown client -- arrives as an
    `error` parameter rather than a code."""
    await sso.get(START)

    response = await sso.get(CALLBACK, params={"error": "access_denied"})

    assert response.status_code == 401, response.text


async def test_no_id_token_means_no_login(sso: AsyncClient, provider: StubProvider) -> None:
    """A provider that treated this as plain OAuth 2.0. There is no way to learn who signed in
    from an access token Apchi cannot read, and asking the userinfo endpoint would accept a
    token minted for something else entirely."""
    provider.omit_id_token = True
    code, state = await _authorize(sso, provider)

    response = await sso.get(CALLBACK, params={"code": code, "state": state})

    assert response.status_code == 401, response.text


async def test_a_login_with_no_readable_username_is_refused(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """Rather than falling back to `sub`, which providers routinely make an opaque blob. §20's
    audit records an actor, and an actor nobody can look up is worse than a refused login."""
    provider.claims.pop("preferred_username")
    code, state = await _authorize(sso, provider)

    response = await sso.get(CALLBACK, params={"code": code, "state": state})

    assert response.status_code == 401, response.text


# --- never degrade ----------------------------------------------------------------------


async def test_an_unreachable_provider_fails_the_login(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """And never falls back. A control plane that dropped its own authentication because an
    identity provider was down would look like it was working."""
    configured = _settings(settings, oidc_issuer="http://127.0.0.1:1")
    app = create_app(configured)
    async for http in _serving(app, fake_kubernetes):
        started = await http.get(START)

        assert started.status_code == 401, started.text
        # Told apart from a refused login on purpose: this one is an Operator's to fix, and
        # says nothing about anybody's credentials.
        assert "identity provider" in started.json()["message"]
        assert (await http.get(ME)).status_code == 401
        assert (await http.get("/api/v1/catalogs")).status_code == 401


async def test_a_provider_that_comes_back_is_usable_without_a_restart(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """Discovery is cached on success but never on failure, so the first login after the
    provider recovers works. Caching the failure would need a pod restart to clear it."""
    provider.stop()
    assert (await sso.get(START)).status_code == 401

    provider.start()

    assert (await sso.get(START)).status_code == 307


async def test_sso_mode_has_no_passwords(sso: AsyncClient) -> None:
    """A password endpoint that pretended otherwise would report "wrong password" to somebody
    whose password cannot be wrong."""
    response = await sso.post(LOGIN, json={"username": "jo", "password": "anything"})

    assert response.status_code == 401, response.text
    assert "sso" in response.json()["message"]


async def test_sso_is_refused_in_password_mode(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    app = create_app(settings.model_copy(update={"auth_mode": AuthMode.PASSWORD}))
    async for http in _serving(app, fake_kubernetes):
        response = await http.get(START)

        assert response.status_code == 401, response.text
        assert "password" in response.json()["message"]


async def test_sso_without_its_settings_refuses_rather_than_crashing(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """A misconfigured Cluster has to be able to say so. Refusing at startup instead would
    take `/health` down with the pod, leaving nothing to read the message from."""
    app = create_app(settings.model_copy(update={"auth_mode": AuthMode.SSO}))
    async for http in _serving(app, fake_kubernetes):
        assert (await http.get("/api/v1/health")).status_code in {200, 503}
        assert (await http.get(START)).status_code == 401


# --- the parts that are nobody's business -----------------------------------------------


async def test_the_client_secret_is_never_logged(
    sso: AsyncClient, provider: StubProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """Asserted rather than assumed, which is why the redaction filter runs at every level in
    every environment: it must not depend on the environment the Cluster happens to be in."""
    with caplog.at_level("DEBUG"):
        await _sign_in(sso, provider)

    assert CLIENT_SECRET not in caplog.text


async def test_the_id_token_is_never_logged(
    sso: AsyncClient, provider: StubProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """It is a bearer credential for as long as it is valid, and a refused login is exactly
    when somebody is tempted to log the token that was refused."""
    provider.sign_with_an_unpublished_key = True
    code, state = await _authorize(sso, provider)

    with caplog.at_level("DEBUG"):
        refused = await sso.get(CALLBACK, params={"code": code, "state": state})

    assert refused.status_code == 401, refused.text
    assert provider.minted, "the provider minted nothing, so this asserts nothing"
    for token in provider.minted:
        assert token not in caplog.text


# --- key rotation -----------------------------------------------------------------------


async def test_a_rotated_signing_key_is_picked_up_without_a_restart(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """The JWKS is cached, so a provider that rotates would otherwise break every login until
    the pod restarted. An unknown `kid` refetches it once -- once, because a signature that is
    still wrong afterwards is a forgery, and retrying on every forgery is a request Apchi
    makes to its provider on demand."""
    await _sign_in(sso, provider)
    provider.rotate()

    await _sign_in(sso, provider)

    assert (await sso.get(ME)).json()["subject"] == "jo"


# --- the user rows SSO leaves behind ----------------------------------------------------


async def test_a_disabled_account_cannot_sign_in(sso: AsyncClient, provider: StubProvider) -> None:
    """Apchi's own override, and it has to outlast a login: an Admin locking somebody out of
    this control plane while they still hold a valid account at the identity provider is the
    case it exists for."""
    await _sign_in(sso, provider)
    app = sso._transport.app  # type: ignore[attr-defined]
    await app.state.user_store._collection.update_one({"_id": "jo"}, {"$set": {"disabled": True}})
    sso.cookies.clear()

    code, state = await _authorize(sso, provider)
    response = await sso.get(CALLBACK, params={"code": code, "state": state})

    assert response.status_code == 401, response.text


async def test_an_sso_login_does_not_grant_a_password(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """The mirrored row has no password hash, and `verify` treats a missing hash as
    unmatchable -- so there is no password that signs this person in, not merely no password
    that is known."""
    await _sign_in(sso, provider)

    app = sso._transport.app  # type: ignore[attr-defined]
    mirrored = await app.state.user_store.get("jo")
    assert mirrored is not None
    assert mirrored.password_hash is None
    assert await app.state.user_store.authenticate("jo", "") is None


async def test_an_existing_password_survives_an_sso_login(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """The same person may exist in both modes across a migration, and signing in through one
    must not delete their way in by the other."""
    app = sso._transport.app  # type: ignore[attr-defined]
    await app.state.user_store.create(
        User(username="jo", password_hash=hash_password("the-password"), role="operator")
    )

    await _sign_in(sso, provider)

    assert await app.state.user_store.authenticate("jo", "the-password") is not None


async def test_a_token_minted_under_sso_answers_to_the_claim(
    sso: AsyncClient, provider: StubProvider
) -> None:
    """Tokens (#131) are owned by a person, and SSO mirrors that person into the same
    collection -- so an automation's role follows its owner's groups, with one freshness story
    rather than two."""
    provider.claims["groups"] = ["platform-admins"]
    await _sign_in(sso, provider)
    minted = await sso.post("/api/v1/tokens", json={"name": "nightly"})
    assert minted.status_code == 201, minted.text
    secret = minted.json()["secret"]

    with_token = AsyncClient(
        transport=ASGITransport(app=sso._transport.app),  # type: ignore[attr-defined]
        base_url="http://apchi",
        headers={"authorization": f"Bearer {secret}"},
    )
    async with with_token:
        assert (await with_token.get("/api/v1/admin/maintenance-mode")).status_code == 200

        provider.claims["groups"] = ["some-other-team"]
        sso.cookies.clear()
        await _sign_in(sso, provider)

        demoted = await with_token.get("/api/v1/admin/maintenance-mode")

    assert demoted.status_code == 403, "demoting the owner must demote their automations"

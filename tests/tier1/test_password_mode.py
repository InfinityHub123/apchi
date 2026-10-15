"""Signing in with a password, and the four ways it must refuse.

The sharp edge here is not whether a correct password works. It is that every *wrong* answer
looks identical -- an unknown username, a wrong password, and a disabled account are one
response with one message and, as far as anyone timing it can tell, one duration. Apchi's
users are named platform staff (§3), so an oracle that says which usernames exist is worth
something to whoever is asking.
"""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.auth.passwords import hash_password
from app.auth.users import User
from app.config import AuthMode, Settings
from app.main import create_app
from tests.conftest import FakeKubernetes

PASSWORD = "correct horse battery staple"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"
LOGOUT = "/api/v1/auth/logout"
REFUSED = "That username and password do not match an enabled account."


async def _serving(app: FastAPI, fake_kubernetes: FakeKubernetes) -> AsyncIterator[AsyncClient]:
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=1)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


def _settings(settings: Settings, **overrides: object) -> Settings:
    return settings.model_copy(update={"auth_mode": AuthMode.PASSWORD, **overrides})


@pytest.fixture
async def password_mode(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    """Password mode with no users at all, so each test provisions what it needs."""
    async for http in _serving(create_app(_settings(settings)), fake_kubernetes):
        yield http


async def _add(http: AsyncClient, username: str, role: str, disabled: bool = False) -> None:
    app = http._transport.app  # type: ignore[attr-defined]
    await app.state.user_store.create(
        User(
            username=username,
            password_hash=hash_password(PASSWORD),
            role=role,  # type: ignore[arg-type]
            disabled=disabled,
        )
    )


# --- signing in ---------------------------------------------------------------------------


async def test_a_correct_password_signs_in_and_says_who_you_are(
    password_mode: AsyncClient,
) -> None:
    await _add(password_mode, "jo", "operator")

    response = await password_mode.post(LOGIN, json={"username": "jo", "password": PASSWORD})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "subject": "jo",
        "role": "operator",
        "via": "password",
        "token_id": None,
    }


async def test_the_session_cookie_carries_the_identity(password_mode: AsyncClient) -> None:
    """The cookie is the credential every later request uses, so the proof is a second
    request rather than the login response."""
    await _add(password_mode, "jo", "operator")
    await password_mode.post(LOGIN, json={"username": "jo", "password": PASSWORD})

    response = await password_mode.get(ME)

    assert response.status_code == 200, response.text
    assert response.json()["subject"] == "jo"


async def test_the_cookie_is_http_only(password_mode: AsyncClient) -> None:
    """A session a page's JavaScript can read is a session an injected script can steal."""
    await _add(password_mode, "jo", "operator")

    response = await password_mode.post(LOGIN, json={"username": "jo", "password": PASSWORD})

    assert "httponly" in response.headers["set-cookie"].lower()


async def test_an_operators_session_is_refused_the_admin_surface(
    password_mode: AsyncClient,
) -> None:
    """The role in the session is the role that is enforced, not a claim the caller makes."""
    await _add(password_mode, "jo", "operator")
    await password_mode.post(LOGIN, json={"username": "jo", "password": PASSWORD})

    response = await password_mode.get("/api/v1/admin/maintenance-mode")

    assert response.status_code == 403, response.text


async def test_an_admins_session_reaches_the_admin_surface(password_mode: AsyncClient) -> None:
    await _add(password_mode, "root", "admin")
    await password_mode.post(LOGIN, json={"username": "root", "password": PASSWORD})

    response = await password_mode.get("/api/v1/admin/maintenance-mode")

    assert response.status_code == 200, response.text


# --- the refusals, which must be identical --------------------------------------------------


@pytest.mark.parametrize(
    "username,password",
    [
        ("nobody", PASSWORD),  # no such user
        ("jo", "wrong"),  # wrong password
        ("off", PASSWORD),  # disabled account
    ],
    ids=["unknown-user", "wrong-password", "disabled-account"],
)
async def test_every_refusal_is_the_same_refusal(
    password_mode: AsyncClient, username: str, password: str
) -> None:
    """One status and one message for all three. Telling them apart is how somebody learns
    which of an organisation's platform staff have accounts here."""
    await _add(password_mode, "jo", "operator")
    await _add(password_mode, "off", "operator", disabled=True)

    response = await password_mode.post(LOGIN, json={"username": username, "password": password})

    assert response.status_code == 401, response.text
    assert response.json()["message"] == REFUSED


async def test_an_unknown_username_costs_a_hash_verification(
    password_mode: AsyncClient,
) -> None:
    """Otherwise the response *time* is the oracle the shared message was hiding.

    Asserted as a ratio rather than a threshold: absolute timings on a busy machine are
    noise, but an early return would make the unknown-user path a small fraction of the
    other, not a comparable one.
    """
    import time

    await _add(password_mode, "jo", "operator")

    async def elapsed(username: str) -> float:
        started = time.perf_counter()
        await password_mode.post(LOGIN, json={"username": username, "password": "wrong"})
        return time.perf_counter() - started

    async def fastest(username: str) -> float:
        return min([await elapsed(username) for _ in range(3)])

    await elapsed("jo")  # warm the cached unmatchable hash and the connection
    wrong_password = await fastest("jo")
    unknown_user = await fastest("nobody")

    assert unknown_user > wrong_password * 0.5, (
        f"an unknown username returned in {unknown_user * 1000:.0f}ms against "
        f"{wrong_password * 1000:.0f}ms for a wrong password, which is an enumeration oracle"
    )


async def test_the_password_is_not_logged(
    password_mode: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Asserted rather than assumed. The redaction filter runs at every level in every
    environment precisely so this cannot depend on the environment."""
    await _add(password_mode, "jo", "operator")

    with caplog.at_level("DEBUG"):
        await password_mode.post(LOGIN, json={"username": "jo", "password": PASSWORD})

    assert PASSWORD not in caplog.text


# --- sessions end when they are told to -----------------------------------------------------


async def test_logout_stops_the_session_working(password_mode: AsyncClient) -> None:
    """Immediately, not at expiry. That is the whole reason sessions are rows rather than
    signed cookies."""
    await _add(password_mode, "jo", "operator")
    await password_mode.post(LOGIN, json={"username": "jo", "password": PASSWORD})

    assert (await password_mode.post(LOGOUT)).status_code == 204
    assert (await password_mode.get(ME)).status_code == 401


async def test_disabling_a_user_kills_their_live_session(password_mode: AsyncClient) -> None:
    """On the next request, without touching their session and without their cooperation.

    The role and the right to be here come from the user on every request, not from the
    session row. Storing them on the session would have made "disable this person now" depend
    on also remembering to delete their sessions.
    """
    await _add(password_mode, "jo", "operator")
    await password_mode.post(LOGIN, json={"username": "jo", "password": PASSWORD})
    assert (await password_mode.get(ME)).status_code == 200

    app = password_mode._transport.app  # type: ignore[attr-defined]
    await app.state.user_store._collection.update_one({"_id": "jo"}, {"$set": {"disabled": True}})

    assert (await password_mode.get(ME)).status_code == 401


async def test_a_promoted_user_gets_the_new_role_on_their_next_request(
    password_mode: AsyncClient,
) -> None:
    """The other half of reading the user rather than the session: a role change does not
    wait for a sign-in."""
    await _add(password_mode, "jo", "operator")
    await password_mode.post(LOGIN, json={"username": "jo", "password": PASSWORD})
    assert (await password_mode.get("/api/v1/admin/maintenance-mode")).status_code == 403

    app = password_mode._transport.app  # type: ignore[attr-defined]
    await app.state.user_store._collection.update_one({"_id": "jo"}, {"$set": {"role": "admin"}})

    assert (await password_mode.get("/api/v1/admin/maintenance-mode")).status_code == 200
    assert (await password_mode.get(ME)).json()["role"] == "admin"


async def test_an_expired_session_is_refused_before_the_ttl_index_reaps_it(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The TTL index runs on its own schedule -- about once a minute -- so expiry has to be
    checked on read as well, or a session outlives its own lifetime by longer than anyone
    would accept."""
    app = create_app(_settings(settings))
    async for http in _serving(app, fake_kubernetes):
        value = await app.state.session_store.create("jo", "password", timedelta(seconds=-1))
        http.cookies.set("apchi_session", value)

        assert (await http.get(ME)).status_code == 401


# --- the bootstrap admin ---------------------------------------------------------------------


async def test_the_initial_admin_is_created_on_an_empty_cluster(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    configured = _settings(
        settings,
        auth_initial_admin_username="root",
        auth_initial_admin_password_hash=hash_password(PASSWORD),
    )
    async for http in _serving(create_app(configured), fake_kubernetes):
        response = await http.post(LOGIN, json={"username": "root", "password": PASSWORD})

        assert response.status_code == 200, response.text
        assert response.json()["role"] == "admin"


async def test_the_initial_admin_never_touches_an_existing_user(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The property that makes it safe to leave in the chart forever.

    Without it the setting is a back door that looks like configuration: every restart would
    resurrect an Admin somebody deleted, or reset a password somebody changed.
    """
    first = _settings(
        settings,
        auth_initial_admin_username="root",
        auth_initial_admin_password_hash=hash_password(PASSWORD),
    )
    app = create_app(first)
    async for http in _serving(app, fake_kubernetes):
        await http.post(LOGIN, json={"username": "root", "password": PASSWORD})

    # Same database, a different hash in the setting, as if somebody edited values.
    changed = first.model_copy(
        update={"auth_initial_admin_password_hash": hash_password("something else")}
    )
    async for http in _serving(create_app(changed), fake_kubernetes):
        still_works = await http.post(LOGIN, json={"username": "root", "password": PASSWORD})
        the_new_one = await http.post(
            LOGIN, json={"username": "root", "password": "something else"}
        )

        assert still_works.status_code == 200, "the stored password was replaced on restart"
        assert the_new_one.status_code == 401, "the setting overwrote an existing account"


async def test_no_initial_admin_means_no_users(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """A password-mode Cluster with nothing configured has nobody who can sign in, and says
    so by refusing rather than by inventing an Admin."""
    async for http in _serving(create_app(_settings(settings)), fake_kubernetes):
        response = await http.post(LOGIN, json={"username": "admin", "password": PASSWORD})

        assert response.status_code == 401, response.text


# --- the other modes -------------------------------------------------------------------------


async def test_login_is_refused_in_none_mode(client: AsyncClient) -> None:
    """There are no passwords to check, and an endpoint that pretended otherwise would
    report "wrong password" to somebody whose password cannot be wrong."""
    response = await client.post(LOGIN, json={"username": "admin", "password": PASSWORD})

    assert response.status_code == 401, response.text
    assert "none" in response.json()["message"]

"""Tokens for automations.

The interesting properties are all about what happens *after* minting: the secret is
unrecoverable, the role is the owner's current one rather than the one they had, and revoking
takes effect on the next request. A token that only worked would be easy; one that stops
working when it should is the point.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.auth.passwords import hash_password
from app.auth.tokens import PREFIX, digest
from app.auth.users import User
from app.config import AuthMode, Settings
from app.main import create_app
from tests.conftest import FakeKubernetes

PASSWORD = "correct horse battery staple"
TOKENS = "/api/v1/tokens"
ME = "/api/v1/auth/me"
LOGIN = "/api/v1/auth/login"
ADMIN_SURFACE = "/api/v1/admin/maintenance-mode"


async def _serving(app: FastAPI, fake_kubernetes: FakeKubernetes) -> AsyncIterator[AsyncClient]:
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=1)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


@pytest.fixture
async def password_mode(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings.model_copy(update={"auth_mode": AuthMode.PASSWORD}))
    async for http in _serving(app, fake_kubernetes):
        yield http


async def _user(http: AsyncClient, username: str, role: str) -> None:
    app = http._transport.app  # type: ignore[attr-defined]
    await app.state.user_store.create(
        User(username=username, password_hash=hash_password(PASSWORD), role=role)  # type: ignore[arg-type]
    )


async def _signed_in(http: AsyncClient, username: str, role: str) -> None:
    await _user(http, username, role)
    response = await http.post(LOGIN, json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text


async def _minted(http: AsyncClient, name: str = "nightly", **extra: object) -> dict:
    response = await http.post(TOKENS, json={"name": name, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def _bearing(http: AsyncClient, secret: str) -> dict[str, str]:
    """A request as the token and nothing else: the session cookie is cleared, so what
    authenticates is unambiguous."""
    http.cookies.clear()
    return {"Authorization": f"Bearer {secret}"}


# --- minting and using ----------------------------------------------------------------------


async def test_a_minted_token_authenticates_as_its_owner(password_mode: AsyncClient) -> None:
    await _signed_in(password_mode, "jo", "operator")
    minted = await _minted(password_mode)

    response = await password_mode.get(ME, headers=_bearing(password_mode, minted["secret"]))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "subject": "jo",
        "role": "operator",
        "via": "token",
        "token_id": minted["token"]["id"],
    }


async def test_the_secret_looks_like_a_secret(password_mode: AsyncClient) -> None:
    """The prefix is what lets a secret scanner, and Apchi's own log filter, recognise one."""
    await _signed_in(password_mode, "jo", "operator")

    minted = await _minted(password_mode)

    assert minted["secret"].startswith(PREFIX)
    assert len(minted["secret"]) > 40


async def test_the_secret_is_never_recoverable(password_mode: AsyncClient) -> None:
    """Asserted against the stored document, not against the API: the claim is that the
    secret is not in the database, so looking only at responses would not test it."""
    await _signed_in(password_mode, "jo", "operator")
    minted = await _minted(password_mode)
    app = password_mode._transport.app  # type: ignore[attr-defined]

    stored = await app.state.token_store._collection.find_one({"_id": minted["token"]["id"]})

    assert minted["secret"] not in str(stored)
    assert stored["token_digest"] == digest(minted["secret"])
    assert (await password_mode.get(TOKENS)).json()["items"][0].get("secret") is None


async def test_using_a_token_records_when(password_mode: AsyncClient) -> None:
    """The question an Operator asks before revoking a token nobody claims: is it still
    being used?"""
    await _signed_in(password_mode, "jo", "operator")
    minted = await _minted(password_mode)
    assert minted["token"]["last_used_at"] is None

    await password_mode.get(ME, headers=_bearing(password_mode, minted["secret"]))

    listed = await password_mode.get(TOKENS, headers=_bearing(password_mode, minted["secret"]))
    assert listed.json()["items"][0]["last_used_at"] is not None


# --- the role is live -----------------------------------------------------------------------


async def test_an_admins_token_reaches_the_admin_surface(password_mode: AsyncClient) -> None:
    await _signed_in(password_mode, "root", "admin")
    minted = await _minted(password_mode)

    response = await password_mode.get(
        ADMIN_SURFACE, headers=_bearing(password_mode, minted["secret"])
    )

    assert response.status_code == 200, response.text


async def test_demoting_the_owner_demotes_the_token(password_mode: AsyncClient) -> None:
    """The reason the token is opaque rather than signed. A role frozen into a claim at
    minting time is a token that keeps Admin access after its owner loses it -- which is the
    thing nobody wants to discover during an incident."""
    await _signed_in(password_mode, "root", "admin")
    minted = await _minted(password_mode)
    headers = _bearing(password_mode, minted["secret"])
    assert (await password_mode.get(ADMIN_SURFACE, headers=headers)).status_code == 200

    app = password_mode._transport.app  # type: ignore[attr-defined]
    await app.state.user_store._collection.update_one(
        {"_id": "root"}, {"$set": {"role": "operator"}}
    )

    assert (await password_mode.get(ADMIN_SURFACE, headers=headers)).status_code == 403
    assert (await password_mode.get(ME, headers=headers)).json()["role"] == "operator"


async def test_disabling_the_owner_kills_the_token(password_mode: AsyncClient) -> None:
    """Without anybody having to find and revoke their tokens first."""
    await _signed_in(password_mode, "jo", "operator")
    minted = await _minted(password_mode)
    headers = _bearing(password_mode, minted["secret"])

    app = password_mode._transport.app  # type: ignore[attr-defined]
    await app.state.user_store._collection.update_one({"_id": "jo"}, {"$set": {"disabled": True}})

    assert (await password_mode.get(ME, headers=headers)).status_code == 401


# --- refusals -------------------------------------------------------------------------------


async def test_a_revoked_token_stops_working(password_mode: AsyncClient) -> None:
    await _signed_in(password_mode, "jo", "operator")
    minted = await _minted(password_mode)
    token_id = minted["token"]["id"]

    assert (await password_mode.delete(f"{TOKENS}/{token_id}")).status_code == 204

    response = await password_mode.get(ME, headers=_bearing(password_mode, minted["secret"]))
    assert response.status_code == 401, response.text


async def test_revoking_is_recorded_rather_than_deleted(password_mode: AsyncClient) -> None:
    """So a token id in an audit trail still resolves to something that says what it was."""
    await _signed_in(password_mode, "jo", "operator")
    minted = await _minted(password_mode)

    await password_mode.delete(f"{TOKENS}/{minted['token']['id']}")

    listed = (await password_mode.get(TOKENS)).json()["items"]
    assert len(listed) == 1
    assert listed[0]["revoked_at"] is not None


async def test_an_expired_token_is_refused(password_mode: AsyncClient) -> None:
    await _signed_in(password_mode, "jo", "operator")
    minted = await _minted(password_mode, expires_in_days=1)
    app = password_mode._transport.app  # type: ignore[attr-defined]
    await app.state.token_store._collection.update_one(
        {"_id": minted["token"]["id"]},
        {"$set": {"expires_at": "2020-01-01T00:00:00Z"}},
    )

    response = await password_mode.get(ME, headers=_bearing(password_mode, minted["secret"]))

    assert response.status_code == 401, response.text


async def test_a_made_up_token_is_refused(password_mode: AsyncClient) -> None:
    await _signed_in(password_mode, "jo", "operator")

    response = await password_mode.get(
        ME, headers=_bearing(password_mode, f"{PREFIX}not-a-real-token-at-all")
    )

    assert response.status_code == 401, response.text


async def test_a_bearer_header_that_is_not_an_apchi_token_falls_through(
    password_mode: AsyncClient,
) -> None:
    """Somebody else's bearer token is not Apchi's problem to interpret: the session is still
    what authenticates, so a proxy adding its own Authorization header does not lock a signed-in
    Operator out."""
    await _signed_in(password_mode, "jo", "operator")

    response = await password_mode.get(ME, headers={"Authorization": "Bearer something.else"})

    assert response.status_code == 200, response.text
    assert response.json()["via"] == "password"


# --- scoping --------------------------------------------------------------------------------


async def test_you_only_see_your_own_tokens(password_mode: AsyncClient) -> None:
    await _signed_in(password_mode, "jo", "operator")
    await _minted(password_mode, "jos token")
    await _user(password_mode, "sam", "operator")
    app = password_mode._transport.app  # type: ignore[attr-defined]
    await app.state.token_store.create("sam", "sams token")

    listed = (await password_mode.get(TOKENS)).json()["items"]

    assert [t["name"] for t in listed] == ["jos token"]


async def test_you_cannot_revoke_somebody_elses_token(password_mode: AsyncClient) -> None:
    """Reported as not found rather than forbidden: "that is not yours" would confirm the id
    exists, which is the one thing an id is worth knowing."""
    await _signed_in(password_mode, "jo", "operator")
    await _user(password_mode, "sam", "operator")
    app = password_mode._transport.app  # type: ignore[attr-defined]
    theirs = await app.state.token_store.create("sam", "sams token")

    response = await password_mode.delete(f"{TOKENS}/{theirs.token.id}")

    assert response.status_code == 404, response.text
    assert (await app.state.token_store.get(theirs.token.id)).revoked_at is None


async def test_minting_needs_an_authenticated_caller(client: AsyncClient) -> None:
    """`none` mode has nobody to own one, and a token owned by "anonymous" would be a
    credential that outlives the mode it was created in."""
    response = await client.post(TOKENS, json={"name": "nightly"})

    assert response.status_code == 422, response.text
    assert "none" in response.json()["message"]


# --- logging --------------------------------------------------------------------------------


async def test_a_token_is_redacted_from_logs_by_value(
    password_mode: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The filter catches secrets by field name, which a bare value sitting in a message has
    no benefit of. The `apchi_` prefix is what makes a by-value rule possible."""
    import logging as stdlib_logging

    await _signed_in(password_mode, "jo", "operator")
    minted = await _minted(password_mode)

    with caplog.at_level("DEBUG"):
        stdlib_logging.getLogger("app.test").info("called with %s", minted["secret"])
        await password_mode.get(ME, headers=_bearing(password_mode, minted["secret"]))

    assert minted["secret"] not in caplog.text

"""The one question a caller with no credential may ask.

A UI cannot tell `password` mode from `sso` mode by probing: `/auth/me` answers 401 in both,
and `/auth/login` and `/auth/sso/login` each refuse in the other's mode. The only difference
is the prose of the refusal, so without this endpoint a client would have to branch on an
error message -- which makes that message a contract nobody meant to publish.

These tests pin the two properties that matter: it answers without a credential in every
mode, and it answers nothing else.
"""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.config import AuthMode, Settings
from app.main import create_app
from tests.conftest import FakeKubernetes

SIGN_IN = "/api/v1/auth/sign-in"
ME = "/api/v1/auth/me"


async def _client(
    settings: Settings, fake_kubernetes: FakeKubernetes, mode: AuthMode
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings.model_copy(update={"auth_mode": mode}))
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(url="http://127.0.0.1:1")
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


@pytest.mark.parametrize("mode", [AuthMode.NONE, AuthMode.PASSWORD, AuthMode.SSO])
async def test_it_answers_in_every_mode_without_a_credential(
    settings: Settings, fake_kubernetes: FakeKubernetes, mode: AuthMode
) -> None:
    """The whole point: a caller who cannot authenticate still learns how to."""
    async for http in _client(settings, fake_kubernetes, mode):
        response = await http.get(SIGN_IN)

        assert response.status_code == 200
        assert response.json() == {"mode": mode.value}


async def test_password_mode_refuses_me_but_still_says_how_to_sign_in(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The asymmetry this endpoint exists for, asserted rather than assumed."""
    async for http in _client(settings, fake_kubernetes, AuthMode.PASSWORD):
        assert (await http.get(ME)).status_code == 401
        assert (await http.get(SIGN_IN)).json() == {"mode": "password"}


async def test_it_discloses_the_mode_and_nothing_else(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """No issuer, no client id, no hint about which accounts exist.

    The mode is already knowable by trying both sign-in routes; anything more would not be.
    """
    async for http in _client(settings, fake_kubernetes, AuthMode.SSO):
        body = (await http.get(SIGN_IN)).json()

        assert list(body) == ["mode"]

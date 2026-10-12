"""Who may call what.

§19's authentication arrives one mode at a time (#128); this is the seam all of them resolve
to. What matters here is coverage rather than any one route: a route that should be Admin-only
and is merely authenticated is a hole nobody notices, because it works. So the gates are
asserted over **every** path in the published contract, not a sample, and the one deliberate
exception is named.
"""

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.api.deps import caller
from app.auth.identity import NOBODY, Identity
from app.config import AuthMode, Settings
from app.main import create_app
from tests.conftest import FakeKubernetes

#: The paths with no gate, and why each one is allowed to be.
#:
#: /health is the chart's readiness *and* liveness probe, so a condition an Operator must log
#: in to clear would crashloop the pod they need alive to reach it -- the argument
#: api/health.py already makes about what may go in it.
#:
#: /auth/login is how a caller gets an identity, so it cannot require one. It is covered by
#: test_password_mode.py instead, which is the stricter test: not "does it refuse nobody" but
#: "does it refuse the wrong password, the unknown user and the disabled account alike".
OPEN = {"/api/v1/health", "/api/v1/auth/login"}

OPERATOR = Identity(subject="jo", role="operator", via="password")

#: Values that satisfy path parameters, so a request reaches the gate rather than stopping at
#: 422. The gate has to be what refuses these, which is the whole point of the assertion.
PARAMS = {
    "number": "1",
    "name": "anything",
    "key": "anything",
    "path": "anything",
    "section": "catalogs",
    "resource": "anything",
    "apply_id": "anything",
    "validation_id": "anything",
}

#: Read from the committed contract rather than from `app.routes`, which this FastAPI
#: represents as private `_IncludedRouter` objects -- a test reaching into those breaks on an
#: upgrade for no reason. The contract is also the right source: the gates are a promise about
#: the published surface, and CI already fails when it drifts from the app.
CONTRACT = Path(__file__).resolve().parents[2] / "openapi.json"


def _operations() -> Iterator[tuple[str, str]]:
    paths: dict[str, Any] = json.loads(CONTRACT.read_text())["paths"]
    for path, operations in paths.items():
        for method in operations:
            if method in {"get", "post", "put", "patch", "delete"}:
                yield method, path


def _concrete(path: str) -> str:
    for parameter, value in PARAMS.items():
        path = path.replace(f"{{{parameter}}}", value)
    assert "{" not in path, f"no stand-in for a parameter in {path}"
    return path


async def _status(http: AsyncClient, method: str, path: str) -> int:
    response = await http.request(method, _concrete(path), json={})
    return response.status_code


async def _serving(app: FastAPI, fake_kubernetes: FakeKubernetes) -> AsyncIterator[AsyncClient]:
    """The `client` fixture's wiring, over an app the caller built.

    The lifespan matters: the stores live on `app.state`, and without it a gated route fails
    on a missing store rather than on the gate -- which would make these tests pass for the
    wrong reason.
    """
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=1)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


@pytest.fixture
async def unauthenticatable(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    """An app in a mode whose backend does not exist yet.

    Every mode but `none` currently resolves nobody, which makes this the cheapest way to
    prove a gate is present on a route: if it were missing, the route would answer.
    """
    app = create_app(settings.model_copy(update={"auth_mode": AuthMode.PASSWORD}))
    async for http in _serving(app, fake_kubernetes):
        yield http


@pytest.fixture
async def as_operator(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    """An app whose caller is an authenticated Operator.

    Overridden rather than logged in, because no mode can produce an Operator yet (#130).
    What is under test is the gate, not the credential.
    """
    app = create_app(settings)
    app.dependency_overrides[caller] = lambda: OPERATOR
    async for http in _serving(app, fake_kubernetes):
        yield http


def test_nobody_is_an_admin() -> None:
    """`none` mode resolves every caller to an Admin, and the choice is load-bearing.

    Every endpoint is open today, so resolving an unauthenticated caller to an Operator would
    *remove* access to /admin/* that exists now -- breaking installs in the name of securing
    them. A Cluster that asks nobody to authenticate cannot tell the two roles apart anyway.
    """
    assert NOBODY.role == "admin"
    assert NOBODY.is_admin
    assert NOBODY.via == "none"


def test_the_contract_is_not_empty() -> None:
    """The coverage tests below iterate the contract, so an unreadable or empty one would
    make them pass by testing nothing."""
    assert sum(1 for _ in _operations()) > 40


async def test_every_route_but_health_requires_an_identity(
    unauthenticatable: AsyncClient,
) -> None:
    """The coverage assertion. A route added later with no gate fails here rather than
    shipping open, which is why this walks the contract instead of naming routes."""
    ungated = [
        f"{method.upper()} {path}"
        for method, path in _operations()
        if path not in OPEN and await _status(unauthenticatable, method, path) != 401
    ]

    assert ungated == [], f"these answer without an identity: {ungated}"


async def test_health_needs_no_identity(unauthenticatable: AsyncClient) -> None:
    """Deliberately open, even in a mode nobody can authenticate against. An authenticated
    liveness probe is a pod that kills itself."""
    response = await unauthenticatable.get("/api/v1/health")

    assert response.status_code in {200, 503}, response.text


async def test_an_operator_is_refused_every_admin_route(as_operator: AsyncClient) -> None:
    """All of /admin/*, not a sample: these inject low-level configuration and freeze mutation
    (§3), and PUT /admin/permissions/enforcement can open a Cluster to every identity without
    a grant."""
    reachable = [
        f"{method.upper()} {path}"
        for method, path in _operations()
        if "/admin/" in path and await _status(as_operator, method, path) != 403
    ]

    assert reachable == [], f"an Operator reached these Admin routes: {reachable}"


async def test_an_operator_reaches_the_operator_surface(as_operator: AsyncClient) -> None:
    """The other half: a gate must not refuse the role it exists to admit."""
    catalogs = await as_operator.get("/api/v1/catalogs")
    review = await as_operator.get("/api/v1/review")

    assert catalogs.status_code == 200, catalogs.text
    assert review.status_code == 200, review.text


async def test_an_admin_reaches_the_admin_surface(client: AsyncClient) -> None:
    """`none` mode, the default, resolves to an Admin -- so the existing suite exercising
    /admin/* is itself the proof this slice changed no behaviour."""
    response = await client.get("/api/v1/admin/maintenance-mode")

    assert response.status_code == 200, response.text

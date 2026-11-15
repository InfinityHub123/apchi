"""What an Operator can read about the Cluster without applying anything.

§21 requires the Cluster's health and the committed Snapshot to be visible at all times, and
before this there was nowhere to read either: `/health` is Apchi's own liveness and the
chart's probe, and a Verification only happens inside an Apply. These tests pin the two
properties that make the answer honest -- the polled route asks Trino nothing, and the last
Verification is reported as history rather than as a reading of now.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.api.deps import caller
from app.auth.identity import Identity
from app.config import AuthMode, Settings
from app.main import create_app
from app.pipeline.applies import TERMINAL
from tests.conftest import FakeKubernetes

ADMIN = Identity(subject="root", role="admin", via="password")
OPERATOR = Identity(subject="jo", role="operator", via="password")

CATALOG = {"name": "scratch", "connector": "memory", "properties": {}}
LISTENER = {
    "name": "audit",
    "type": "http",
    "properties": {"http-event-listener.connect-ingest-uri": "http://collector:8080/e"},
}
STATE = "/api/v1/cluster"
PROBE = "/api/v1/cluster/probe"


async def _serving(
    settings: Settings, fake: FakeKubernetes, who: Identity
) -> AsyncIterator[AsyncClient]:
    """Two callers against one deployment, so both read the same Maintenance Mode.

    The role is overridden rather than logged in, because no mode can produce an Operator
    yet (#130) and what is under test is which role may read this, not the credential.
    """
    app = create_app(settings)
    app.dependency_overrides[caller] = lambda: who
    app.state.kubernetes = fake
    app.state.trino = Trino(url="http://127.0.0.1:1")
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


@pytest.fixture
async def operator_client(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    async for http in _serving(settings, fake_kubernetes, OPERATOR):
        yield http


@pytest.fixture
async def admin_client(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    async for http in _serving(settings, fake_kubernetes, ADMIN):
        yield http


@pytest.fixture
async def unauthenticatable(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    """`password` mode with no users: nothing can authenticate, so a route that answers has
    no gate on it."""
    app = create_app(settings.model_copy(update={"auth_mode": AuthMode.PASSWORD}))
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(url="http://127.0.0.1:1")
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


async def _applied(client: AsyncClient) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    for _ in range(1200):
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError(f"Apply never settled: {record}")


async def test_a_cluster_nothing_has_been_applied_to_says_so(client: AsyncClient) -> None:
    """Null rather than a zero or an empty object: "no Snapshot yet" is a state a design has
    to render, and it is not the same as Snapshot 0."""
    state = (await client.get(STATE)).json()

    assert state["snapshot"] is None
    assert state["snapshot_at"] is None
    assert state["last_apply"] is None
    assert state["maintenance"]["engaged"] is False


async def test_it_reports_the_committed_snapshot_and_the_apply_that_made_it(
    applying_client: AsyncClient,
) -> None:
    await applying_client.post("/api/v1/catalogs", json=CATALOG)
    record = await _applied(applying_client)
    assert record["stage"] == "succeeded", record.get("failure_reason")

    state = (await applying_client.get(STATE)).json()

    assert state["snapshot"] == 1
    assert state["snapshot_at"] is not None
    assert state["last_apply"]["id"] == record["id"]
    assert state["last_apply"]["snapshot"] == 1
    assert state["last_apply"]["verified"] is True
    assert state["last_apply"]["finished_at"] is not None


async def test_an_apply_that_never_reached_verification_reports_null_not_false(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The distinction the field exists for. A Validation failure means nothing was
    delivered and nothing was checked, which is not the same answer as "checked and
    unhealthy" -- and a design that renders both as a red light is lying about one."""
    fake_kubernetes.pod_problem = "the validation pod failed"
    await applying_client.post("/api/v1/event-listeners", json=LISTENER)

    record = await _applied(applying_client)
    assert record["stage"] == "failed", record

    state = (await applying_client.get(STATE)).json()

    assert state["last_apply"]["verified"] is None
    assert state["last_apply"]["stage"] == "failed"
    assert state["snapshot"] is None, "nothing was committed"


async def test_the_polled_route_asks_trino_nothing(client: AsyncClient) -> None:
    """The whole reason it is separate from the probe.

    `client` has no reachable coordinator at all, so a route that touched Trino would hang
    on it or report it. This one answers at once, which is what makes it safe to poll from
    a console somebody leaves open.
    """
    response = await client.get(STATE)

    assert response.status_code == 200
    assert "coordinator" not in response.json()


async def test_the_probe_reports_an_unreachable_coordinator_rather_than_failing(
    client: AsyncClient,
) -> None:
    """A 503 here would make "Trino is down" indistinguishable from "Apchi is broken", which
    is the one distinction somebody opens this endpoint to make."""
    response = await client.get(PROBE)

    assert response.status_code == 200
    assert response.json()["coordinator"] == "unreachable"
    assert response.json()["workers"] is None
    assert response.json()["at"] is not None


async def test_the_probe_reaches_a_real_coordinator(applying_client: AsyncClient) -> None:
    response = await applying_client.get(PROBE)

    assert response.json()["coordinator"] == "up"
    assert response.json()["workers"] is None, "not counted unless asked for"


async def test_workers_are_counted_only_when_asked_for(applying_client: AsyncClient) -> None:
    """A single-node Trino is its own coordinator and has no workers, so 0 is the honest
    answer -- and it is reported as 0 rather than null, because a Cluster with no workers is
    a real state and not an unknown one."""
    response = await applying_client.get(PROBE, params={"workers": "true"})

    assert response.json()["coordinator"] == "up"
    assert response.json()["workers"] == 0


async def test_an_unreachable_coordinator_leaves_the_worker_count_unknown(
    client: AsyncClient,
) -> None:
    response = await client.get(PROBE, params={"workers": "true"})

    assert response.json()["coordinator"] == "unreachable"
    assert response.json()["workers"] is None, "unknown, not zero"


async def test_maintenance_mode_is_readable_by_an_operator(
    operator_client: AsyncClient, admin_client: AsyncClient
) -> None:
    """The point of putting it here. An Operator cannot read `/admin/maintenance-mode`, so
    before this the only way to discover that editing was disabled was to type a change and
    have it refused."""
    assert (await operator_client.get("/api/v1/admin/maintenance-mode")).status_code == 403

    engaged = await admin_client.put(
        "/api/v1/admin/maintenance-mode",
        json={"engaged": True, "reason": "Upgrading the platform."},
    )
    assert engaged.status_code == 200

    state = (await operator_client.get(STATE)).json()

    assert state["maintenance"]["engaged"] is True
    assert state["maintenance"]["reason"] == "Upgrading the platform."
    assert state["maintenance"]["engaged_by"] == "admin"
    assert state["maintenance"]["engaged_at"] is not None


async def test_it_needs_an_identity(unauthenticatable: AsyncClient) -> None:
    """Not open like `/health`. It reports what the Cluster is running and why editing is
    disabled, which is not for anyone who can reach the port."""
    assert (await unauthenticatable.get(STATE)).status_code == 401
    assert (await unauthenticatable.get(PROBE)).status_code == 401

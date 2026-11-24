"""Whether the Cluster still holds what Apchi committed to it.

A Snapshot is a record of what was delivered, not a guarantee it is still there. Two things
move the Cluster underneath it (§14) -- a Secret edited by hand, and an Admin value that
moves no Section at all -- and before this the only trace of either was a Section named in
Review's `rollout_sections` with nothing in the diff to account for it.

These tests pin the three answers that make the read honest: a Section Apchi cannot compare
says so instead of claiming to be in sync, a Section it could not read says so instead of
being counted as clean, and the roll-up never reports an all-clear it did not earn.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.config import AuthMode, Settings
from app.main import create_app
from app.pipeline.applies import TERMINAL
from app.sections.event_listeners.generator import FILE_KEY
from tests.conftest import FakeKubernetes

CATALOG = {"name": "scratch", "connector": "memory", "properties": {}}
LISTENER = {
    "name": "audit",
    "type": "http",
    "properties": {"http-event-listener.connect-ingest-uri": "http://collector:8080/e"},
}
LISTENER_SECRET = "trino-event-listener"
DRIFT = "/api/v1/cluster/drift"

#: Registry order, which is the order the response promises.
SECTIONS = [
    "catalogs",
    "client_certificates",
    "certificate_mapping",
    "event_listeners",
    "permissions",
    "resource_groups",
]


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
        AsyncClient(transport=ASGITransport(app=app), base_url="https://apchi") as http,
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


def _by_section(drift: dict) -> dict[str, str]:
    return {section["section"]: section["status"] for section in drift["sections"]}


async def test_a_cluster_nothing_was_applied_to_has_nothing_to_have_drifted_from(
    client: AsyncClient,
) -> None:
    """Not `in_sync`. Drift is a divergence from a committed state, and there is not one --
    so every Section is undetermined and the verdict is null rather than an all-clear."""
    drift = (await client.get(DRIFT)).json()

    assert drift["snapshot"] is None
    assert drift["diverged"] is None
    assert set(_by_section(drift).values()) == {"unknown"}


async def test_every_section_is_reported_in_registry_order(client: AsyncClient) -> None:
    """All six, whether they drifted or not, and in the order Review reports them -- so a
    client joins the two without sorting either."""
    drift = (await client.get(DRIFT)).json()

    assert [section["section"] for section in drift["sections"]] == SECTIONS


async def test_each_section_says_whether_reconciling_it_restarts_trino(
    client: AsyncClient,
) -> None:
    """Read off Apchi's own registry, so a client does not keep a second copy of it -- the
    reason #210 put `rollout_sections` on the Apply cost."""
    drift = (await client.get(DRIFT)).json()

    restarts = {s["section"]: s["restarts_coordinator"] for s in drift["sections"]}
    assert restarts == {
        "catalogs": False,
        "client_certificates": False,
        "certificate_mapping": True,
        "event_listeners": True,
        "permissions": False,
        "resource_groups": True,
    }


async def test_catalogs_say_they_cannot_be_compared_rather_than_claiming_to_be_in_sync(
    applying_client: AsyncClient,
) -> None:
    """The one Section no file diff can speak for.

    Catalogs reach Trino as statements, not as a mounted file, so `would_change` has nothing
    to read and would answer "no change" forever. Reporting that as `in_sync` would be an
    all-clear Apchi has not earned, so it is its own status.
    """
    await applying_client.post("/api/v1/catalogs", json=CATALOG)
    record = await _applied(applying_client)
    assert record["stage"] == "succeeded", record.get("failure_reason")

    drift = (await applying_client.get(DRIFT)).json()

    assert drift["snapshot"] == 1
    assert _by_section(drift)["catalogs"] == "not_comparable"


async def test_a_cluster_apchi_just_applied_to_has_not_drifted(
    applying_client: AsyncClient,
) -> None:
    await applying_client.post("/api/v1/event-listeners", json=LISTENER)
    record = await _applied(applying_client)
    assert record["stage"] == "succeeded", record.get("failure_reason")

    drift = (await applying_client.get(DRIFT)).json()

    assert drift["diverged"] is False
    assert _by_section(drift)["event_listeners"] == "in_sync"


async def test_a_secret_edited_by_hand_is_drift_with_nothing_staged(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The case Review can only hint at. Nothing is staged, the Candidate equals its
    Snapshot, and the Cluster is still running something Apchi did not put there."""
    await applying_client.post("/api/v1/event-listeners", json=LISTENER)
    record = await _applied(applying_client)
    assert record["stage"] == "succeeded", record.get("failure_reason")

    fake_kubernetes.secrets[LISTENER_SECRET][FILE_KEY] = "event-listener.name=edited-by-hand"

    drift = (await applying_client.get(DRIFT)).json()
    review = (await applying_client.get("/api/v1/review")).json()

    assert review["has_changes"] is False, "nothing is staged"
    assert drift["diverged"] is True
    assert _by_section(drift)["event_listeners"] == "diverged"


async def test_an_admin_value_moving_drifts_a_section_nobody_touched(
    applying_client: AsyncClient,
) -> None:
    """§14's other cause, and the one no diff can show at all.

    Turning enforcement on stages nothing and moves no Section: it changes what the
    permissions generator renders, so the rules file Apchi would write stops being the one
    the Cluster has mounted. Review reports no changes, because there are none.
    """
    record = await _applied(applying_client)
    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert _by_section((await applying_client.get(DRIFT)).json())["permissions"] == "in_sync"

    enforced = await applying_client.put(
        "/api/v1/admin/permissions/enforcement", json={"enforced": True}
    )
    assert enforced.status_code == 200, enforced.json()

    drift = (await applying_client.get(DRIFT)).json()

    assert (await applying_client.get("/api/v1/review")).json()["has_changes"] is False
    assert _by_section(drift)["permissions"] == "diverged"
    assert drift["diverged"] is True


async def test_a_section_that_could_not_be_read_is_unknown_not_clean(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """And it spoils the verdict rather than being counted as in sync. An all-clear Apchi
    could not verify is not an all-clear, which is why `diverged` is three-valued."""
    record = await _applied(applying_client)
    assert record["stage"] == "succeeded", record.get("failure_reason")

    fake_kubernetes.secret_read_problem = "the API server refused the read"

    response = await applying_client.get(DRIFT)

    assert response.status_code == 200, "an unreadable Cluster is the answer, not an error"
    drift = response.json()
    assert drift["snapshot"] == 1, "there is a Snapshot; it is the Cluster that is unreadable"
    assert drift["diverged"] is None
    statuses = _by_section(drift)
    assert statuses["event_listeners"] == "unknown"
    assert statuses["catalogs"] == "not_comparable", "still permanent, not a read failure"


async def test_it_needs_an_identity(unauthenticatable: AsyncClient) -> None:
    assert (await unauthenticatable.get(DRIFT)).status_code == 401

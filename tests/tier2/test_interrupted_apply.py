"""An Apply interrupted mid-flight, against a real cluster.

The divergence this recovers from is real and specific: Apply writes the catalog Secret
before it issues DDL, so a process that dies in that window leaves a Secret naming a
catalog the coordinator never got. Nothing links the two events -- the catalog simply
appears at the next pod restart, which is why the restart here is the assertion that
matters.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.kubernetes import RealKubernetes
from app.adapters.trino import Trino
from app.config import Settings
from app.main import create_app
from app.pipeline.applies import TERMINAL
from tests.tier2.conftest import (
    CATALOG_SEED_SECRET,
    ForwardedKubernetes,
    PortForward,
    restart_coordinator,
)

pytestmark = pytest.mark.tier2

KEPT = {"name": "kept", "connector": "memory", "properties": {}}
ADDED = {"name": "added", "connector": "memory", "properties": {}}


@asynccontextmanager
async def _apchi(
    settings: Settings, kubernetes: ForwardedKubernetes, forward: PortForward
) -> AsyncIterator[AsyncClient]:
    """One run of Apchi against the real cluster. Leaving and re-entering is a restart."""
    app = create_app(settings)
    app.state.kubernetes = kubernetes
    app.state.trino = Trino(host="127.0.0.1", port=forward.port, user=settings.trino_user)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://apchi") as client,
        app.router.lifespan_context(app),
    ):
        client.app_under_test = app  # type: ignore[attr-defined]
        yield client


async def _succeed(client: AsyncClient, timeout: float = 600.0) -> None:
    started = await client.post("/api/v1/applies")
    apply_id = started.json()["id"]
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            assert record["stage"] == "succeeded", record.get("failure_reason")
            return
        await asyncio.sleep(0.5)
    raise AssertionError("Apply never settled")


async def test_a_restart_mid_apply_leaves_nothing_behind_for_the_next_pod(
    settings: Settings,
    forwarded_kubernetes: ForwardedKubernetes,
    forward: PortForward,
    real_kubernetes: RealKubernetes,
    cluster_state: None,
) -> None:
    async with _apchi(settings, forwarded_kubernetes, forward) as first:
        await first.post("/api/v1/catalogs", json=KEPT)
        await _succeed(first)

        await first.post("/api/v1/catalogs", json=ADDED)
        started = await first.post("/api/v1/applies")
        apply_id = started.json()["id"]
        # Kill it once the durable copy names the new catalog: the DDL may or may not
        # have run, and recovery has to converge either way.
        deadline = asyncio.get_running_loop().time() + 300
        while asyncio.get_running_loop().time() < deadline:
            seed = await real_kubernetes.read_secret(CATALOG_SEED_SECRET)
            if "added.properties" in seed:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the Apply never wrote the Secret")
        await first.app_under_test.state.apply_runner.shutdown()  # type: ignore[attr-defined]

    async with _apchi(settings, forwarded_kubernetes, forward) as restarted:
        record = (await restarted.get(f"/api/v1/applies/{apply_id}")).json()
        assert record["interrupted"] is True
        assert record["rollback"] == "succeeded", record.get("failure_reason")
        assert (await restarted.get("/api/v1/snapshots")).json()[0]["number"] == 1

    assert set(await real_kubernetes.read_secret(CATALOG_SEED_SECRET)) == {"kept.properties"}

    # The assertion that matters: the pod is what would have surfaced the divergence.
    restart_coordinator()
    forward.restart()

    live = await Trino(host="127.0.0.1", port=forward.port).catalogs()
    assert "kept" in live
    assert "added" not in live

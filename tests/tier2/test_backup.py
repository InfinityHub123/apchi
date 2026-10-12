"""The export against a real cluster.

Tier 1 proves the document's shape. The one claim it cannot prove is the provenance: the
Cluster id and the highest Snapshot ever committed come from a ConfigMap, and in Tier 1 that
ConfigMap is a dictionary in the fake. #111 is the reason this test exists -- a fake that
answers more agreeably than Kubernetes does makes a broken Apchi look correct, and this
export's whole job is to be trustworthy afterwards.
"""

import asyncio
import json

import pytest
from httpx import AsyncClient

from app.config import Settings
from app.pipeline.applies import TERMINAL
from app.pipeline.backup import FORMAT
from tests.tier2.conftest import kubectl

pytestmark = pytest.mark.tier2

SCRATCH = {"name": "scratch", "connector": "memory", "properties": {}}


async def _apply(client: AsyncClient, timeout: float = 600.0) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    apply_id = started.json()["id"]
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            assert record["stage"] == "succeeded", record.get("failure_reason")
            return record
        await asyncio.sleep(0.5)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


async def test_the_export_takes_its_provenance_from_a_real_config_map(
    e2e_client: AsyncClient, settings: Settings
) -> None:
    """The Cluster id in the export is the one Kubernetes is holding, read back out of it.

    Compared against `kubectl`, not against what Apchi just wrote: the point is that a
    restore judging a backup by its Cluster id is judging it by something that outlives
    MongoDB, and only a real ConfigMap shows that.
    """
    assert (await e2e_client.post("/api/v1/catalogs", json=SCRATCH)).status_code == 201
    record = await _apply(e2e_client)

    backup = (await e2e_client.get("/api/v1/admin/backup")).json()

    mark = json.loads(kubectl("get", "configmap", settings.high_water_config_map, "-o", "json"))[
        "data"
    ]
    assert backup["format"] == FORMAT
    assert backup["cluster_id"] == mark["cluster_id"]
    assert backup["snapshots_ever"] == int(mark["highest_snapshot"]) == record["snapshot"]
    assert [s["number"] for s in backup["snapshots"]] == [record["snapshot"]]
    assert "scratch" in backup["snapshots"][0]["sections"]["catalogs"]

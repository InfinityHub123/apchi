"""A raw file on a real coordinator, and the one test that proves Trino reads it (§14).

Tier 1 proves the mechanics against a fake Kubernetes: the Secret is written, the mount
appears, the pre-image restores. What a fake cannot prove is the part the feature rests on --
that Trino actually consumes the file. A delivery mechanism that satisfies Apchi's own model
and lands somewhere Trino never looks passes every tier 1 test and does nothing.

That distinction is load-bearing, and it was not what the first version of this file assumed.
An arbitrary `.properties` file in `/etc/trino` is **ignored**. So the mechanics are tested
against a path Trino ignores, and delivery itself is proved against a file Trino reads by
itself when it exists -- `session-property-config.properties` -- now that `config.properties`
and the other files Trino reads at start are Apchi's to render (ADR-0010).
"""

import asyncio
import json
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.adapters.trino import Trino
from app.config import Settings
from tests.tier2.conftest import (
    COORDINATOR,
    SERVER_CONFIG_SECRET,
    PortForward,
    _live_secret,
    kubectl,
    mounted_paths,
)

pytestmark = pytest.mark.tier2

FILES = "/api/v1/admin/files"
ADMIN_APPLIES = "/api/v1/admin/applies"
RAW_VOLUME = "apchi-admin-raw-files"

#: A path Trino does **not** read. Safe for the delivery mechanics, and the fact that nonsense
#: here is harmless is itself asserted below -- assuming otherwise is what made this file
#: wrong first time.
RAW_PATH = "/etc/trino/apchi-raw-test.properties"

#: A file Trino reads by itself when it is there, and refuses to start over when it is wrong:
#: the session property manager's. Two raw files -- this one, and the rules it points at.
SESSION_CONFIG = "/etc/trino/session-property-config.properties"
SESSION_RULES = "/etc/trino/session-properties.json"
SESSION_CONTENT = (
    "session-property-config.configuration-manager=file\n"
    f"session-property-manager.config-file={SESSION_RULES}\n"
)


async def _write(client: AsyncClient, content: str, path: str = RAW_PATH) -> None:
    response = await client.put(f"{FILES}{path}", json={"content": content})
    assert response.status_code == 200, response.text


async def _settled(client: AsyncClient, apply_id: str, timeout: float = 600.0) -> dict:
    """Poll until the Apply reaches a terminal stage."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in ("succeeded", "failed", "incident"):
            return dict(record)
        await asyncio.sleep(0.5)
    raise AssertionError(f"apply {apply_id} never finished")


def _coordinator_pod() -> str:
    pods = json.loads(
        kubectl("get", "pods", "-l", "app.kubernetes.io/component=coordinator", "-o", "json")
    )["items"]
    running = [pod for pod in pods if pod["status"].get("phase") == "Running"]
    assert len(running) == 1, [pod["metadata"]["name"] for pod in pods]
    return str(running[0]["metadata"]["uid"])


async def _admin_apply(client: AsyncClient) -> dict:
    started = await client.post(ADMIN_APPLIES, json={})
    assert started.status_code == 202, started.text
    return await _settled(client, started.json()["id"], timeout=900.0)


async def test_raw_files_from_delivery_through_a_sync_to_removal(
    e2e_client: AsyncClient, forward: PortForward, settings: Settings
) -> None:
    """One Admin Apply carrying every kind of raw file, then what happens to them after.

    One scenario rather than a test per assertion, because each Apply here restarts the
    coordinator and each test pays a restart again to be put back (#300).

    - **Listed back** through a real MongoDB and Kubernetes before anything is applied: paths
      and sizes, never content.
    - **Delivered**: the mount lands on the live Deployment, read back from the object rather
      than from Apchi's own record -- a mechanism that only agrees with itself proves nothing.
    - **Read by Trino**: its own session property manager, configured entirely by two raw
      files, sets a session default for Apchi's identity, and a Server Configuration property
      arrives in the same Apply, for one restart.
    - **A file Trino does not read is harmless.** Nonsense in an arbitrary `.properties` file
      is delivered and changes nothing, which also means Validation cannot catch a typo in a
      path; the listing is what makes that visible. And an Admin Apply makes no Snapshot.
    - **It survives a sync** of the Deployment as the chart renders it, client- and
      server-side, as Argo CD's sync does.
    - **Removed, it loses its mount.** Removing the key is not enough: a subPath mount of an
      absent key becomes a directory Trino dies reading.
    """
    nonsense = "this-is-not-a-trino-property=whatever\n"
    rules = [
        {
            "user": f"{settings.trino_user}.*",
            "sessionProperties": {"query_max_execution_time": "7h"},
        }
    ]
    await _write(e2e_client, nonsense)
    await _write(e2e_client, SESSION_CONTENT, path=SESSION_CONFIG)
    await _write(e2e_client, json.dumps(rules), path=SESSION_RULES)
    listed = {entry["path"]: entry["bytes"] for entry in (await e2e_client.get(FILES)).json()}
    assert listed[RAW_PATH] == len(nonsense.encode())
    assert set(listed) == {RAW_PATH, SESSION_CONFIG, SESSION_RULES}
    response = await e2e_client.put(
        "/api/v1/admin/server-configuration",
        json={"coordinator": {"config.properties": {"query.max-run-time": "5h"}}},
    )
    assert response.status_code == 200, response.text

    record = await _admin_apply(e2e_client)

    assert record["stage"] == "succeeded", record
    assert record["snapshot"] is None, "an Admin Apply creates no Snapshot (§14)"
    assert [event["stage"] for event in record["history"]].count("rolling_out") == 1
    assert {RAW_PATH, SESSION_CONFIG, SESSION_RULES} <= set(mounted_paths(RAW_VOLUME))
    forward.restart()
    trino = Trino(url=f"http://127.0.0.1:{forward.port}", user=settings.trino_user)
    shown = {row[0]: row[1] for row in await trino.query("SHOW SESSION LIKE 'query_max_%'")}
    assert shown["query_max_execution_time"] == "7h", "the raw files were not read"
    assert shown["query_max_run_time"] == "5.00h", "the server configuration was not read"

    server_config = _live_secret(SERVER_CONFIG_SECRET)
    coordinator = str(Path(__file__).resolve().parents[2] / "deploy/trino-dev/10-coordinator.yaml")
    kubectl("apply", "-f", coordinator)
    kubectl("apply", "--server-side", "--force-conflicts", "-f", coordinator)
    kubectl("rollout", "status", COORDINATOR, "--timeout=600s")
    assert {RAW_PATH, SESSION_CONFIG, SESSION_RULES} <= set(mounted_paths(RAW_VOLUME))
    assert _live_secret(SERVER_CONFIG_SECRET) == server_config

    for path in (RAW_PATH, SESSION_CONFIG, SESSION_RULES):
        assert (await e2e_client.delete(f"{FILES}{path}")).status_code == 204
    assert (await _admin_apply(e2e_client))["stage"] == "succeeded"
    assert not {RAW_PATH, SESSION_CONFIG, SESSION_RULES} & set(mounted_paths(RAW_VOLUME))


@pytest.mark.parametrize(
    ("content", "trino_says"),
    [
        ("session-property-config.configuration-manager=nosuch\n", "is not registered"),
        ("nonsense=1\n", "does not contain"),
        (
            "session-property-config.configuration-manager=file\n"
            "session-property-manager.config-file=/etc/trino/missing.json\n",
            "file does not exist",
        ),
    ],
)
async def test_a_raw_file_trino_refuses_is_refused_on_the_probe(
    e2e_client: AsyncClient, content: str, trino_says: str
) -> None:
    """Before this stage a raw file was probed only when some Section had something to probe,
    and on an empty Cluster this would have reached the coordinator."""
    before = _coordinator_pod()
    await _write(e2e_client, content, path=SESSION_CONFIG)

    record = await _admin_apply(e2e_client)

    assert record["stage"] == "failed", record
    assert trino_says in (record["failure_reason"] or ""), record["failure_reason"]
    assert record["rolled_out"] is False
    assert _coordinator_pod() == before
    assert SESSION_CONFIG not in mounted_paths(RAW_VOLUME)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/trino/config.properties",
        "/etc/trino/jvm.config",
        "/etc/trino/node.properties",
        "/etc/trino/log.properties",
        "/etc/trino/catalog-store.properties",
        "/etc/trino/access-control.properties",
    ],
)
async def test_a_raw_file_at_a_file_apchi_renders_is_refused(
    e2e_client: AsyncClient, path: str
) -> None:
    response = await e2e_client.put(f"{FILES}{path}", json={"content": "x=y\n"})

    assert response.status_code == 422, response.text
    assert "/api/v1/admin/server-configuration" in response.json()["message"]

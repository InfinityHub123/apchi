"""A raw file on a real coordinator, and the one test that proves Trino reads it (§14).

Tier 1 proves the mechanics against a fake Kubernetes: the Secret is written, the mount
appears, the pre-image restores. What a fake cannot prove is the part the feature rests on --
that Trino actually consumes the file. A delivery mechanism that satisfies Apchi's own model
and lands somewhere Trino never looks passes every tier 1 test and does nothing.

That distinction is load-bearing, and it was not what the first version of this file assumed.
Trino reads `config.properties`, `node.properties`, `jvm.config` and its catalog files; an
arbitrary `.properties` file in `/etc/trino` is **ignored**. So the mechanics are tested
against a path Trino ignores, and delivery itself is proved against `config.properties`.
"""

import asyncio

import pytest
from httpx import AsyncClient

from app.config import Settings
from tests.tier2.conftest import (
    ForwardedKubernetes,
    PortForward,
    kubectl,
    mounted_paths,
    running_apchi,
)

pytestmark = pytest.mark.tier2

FILES = "/api/v1/admin/files"
ADMIN_APPLIES = "/api/v1/admin/applies"
RAW_VOLUME = "apchi-admin-raw-files"

#: A path Trino does **not** read. Safe for the delivery mechanics, and the fact that nonsense
#: here is harmless is itself asserted below -- assuming otherwise is what made this file
#: wrong first time.
RAW_PATH = "/etc/trino/apchi-raw-test.properties"

#: The file Trino really does read, for the one test that proves delivery reaches it.
CONFIG_PATH = "/etc/trino/config.properties"


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


async def test_a_raw_file_is_listed_back_from_a_real_cluster(e2e_client: AsyncClient) -> None:
    """The round trip through a real MongoDB and a real Kubernetes, before anything is
    applied. Paths and sizes, never content."""
    await _write(e2e_client, "apchi.raw-test=one\n")

    listed = (await e2e_client.get(FILES)).json()

    assert listed == [{"path": RAW_PATH, "bytes": len(b"apchi.raw-test=one\n")}]


async def test_a_raw_file_reaches_the_coordinator(e2e_client: AsyncClient) -> None:
    """The mount lands on the real Deployment, read back from the live object rather than from
    Apchi's own record of what it believes it mounted -- the same reason §11 settles rollback
    as a diff of two durable records: a mechanism that only agrees with itself proves
    nothing."""
    await _write(e2e_client, "apchi.raw-test=two\n")
    started = await e2e_client.post(ADMIN_APPLIES, json={})
    assert started.status_code == 202, started.text
    await _settled(e2e_client, started.json()["id"])

    assert RAW_PATH in mounted_paths(RAW_VOLUME)


async def test_a_removed_raw_file_loses_its_mount(e2e_client: AsyncClient) -> None:
    """Removing the content is not enough. Trino refuses to start when a file it was told to
    read is missing, and a subPath mount of an absent Secret key becomes a directory it dies
    reading -- so the mount has to go, not just the key."""
    await _write(e2e_client, "apchi.raw-test=three\n")
    first = await e2e_client.post(ADMIN_APPLIES, json={})
    await _settled(e2e_client, first.json()["id"])

    assert (await e2e_client.delete(f"{FILES}{RAW_PATH}")).status_code == 204
    second = await e2e_client.post(ADMIN_APPLIES, json={})
    await _settled(e2e_client, second.json()["id"])

    assert RAW_PATH not in mounted_paths(RAW_VOLUME)


async def test_a_file_trino_does_not_read_is_delivered_and_harmless(
    e2e_client: AsyncClient,
) -> None:
    """Worth asserting, because assuming otherwise is what made this file wrong first time.

    An arbitrary `.properties` file in `/etc/trino` is ignored by Trino, so nonsense in one is
    delivered, changes nothing, and the Apply succeeds. That is not a hole in "if it works it
    works" -- the file does work, in that nothing breaks. It does mean **Validation cannot
    catch a typo in a path**: an Admin who means `config.properties` and writes
    `config.propertie` gets a clean Apply and no effect. The listing reporting paths is what
    makes that visible.
    """
    await _write(e2e_client, "this-is-not-a-trino-property=whatever\n")

    started = await e2e_client.post(ADMIN_APPLIES, json={})
    assert started.status_code == 202, started.text
    record = await _settled(e2e_client, started.json()["id"])

    assert record["stage"] == "succeeded", record
    assert record["snapshot"] is None, "an Admin Apply creates no Snapshot (§14)"


async def test_trino_reads_a_raw_config_properties(
    settings: Settings,
    forwarded_kubernetes: ForwardedKubernetes,
    forward: PortForward,
) -> None:
    """The assertion that actually proves delivery: a file Trino **does** read.

    The deployment's own `config.properties` plus one property Trino rejects. Trino then fails
    to start, which is only possible if it read the file Apchi delivered -- a mount landing
    somewhere Trino never looks would leave the Cluster healthy, which is exactly what the
    test above shows happens for an unread path.

    It also exercises the limit §14 records: `config.properties` is the one raw file the probe
    cannot validate, because the probe assembles its own. So this fails on the **Cluster**, and
    Auto Rollback is what puts it back.
    """
    live = kubectl("exec", "deploy/trino-coordinator", "--", "cat", CONFIG_PATH)
    assert "coordinator=true" in live, live

    # A short rollout timeout, because what is under test is that Trino *rejected* the file --
    # not how long Apchi waits for a pod that will never be ready. With the 600s default this
    # test waits out the whole timeout to learn something it knows within seconds, which is
    # the difference between a slow test and an unrunnable one in CI.
    impatient = settings.model_copy(update={"rollout_timeout_seconds": 90.0})
    async with running_apchi(impatient, forwarded_kubernetes, forward) as client:
        await _write(client, f"{live}\nthis-is-not-a-trino-property=whatever\n", path=CONFIG_PATH)
        started = await client.post(ADMIN_APPLIES, json={})
        assert started.status_code == 202, started.text
        record = await _settled(client, started.json()["id"], timeout=300.0)

    assert record["stage"] in ("failed", "incident"), (
        "Trino started cleanly with a config.properties it should have rejected, which means "
        f"it never read the file Apchi delivered: {record}"
    )

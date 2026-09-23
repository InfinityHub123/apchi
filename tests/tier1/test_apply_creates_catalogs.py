"""The tracer bullet: stage a Catalog, Apply, and find it live in Trino with an
immutable Snapshot recorded.

MongoDB and Trino are real here. Only Kubernetes is faked, so the Secret patch is
observable without a cluster.
"""

import asyncio

from httpx import AsyncClient

MEMORY = {"name": "scratch", "connector": "memory", "properties": {}}
SECRET = "trino-catalog-seed"


async def _settled(client: AsyncClient, apply_id: str, timeout: float = 30.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in {"succeeded", "failed"}:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


async def _apply(client: AsyncClient) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202
    return await _settled(client, started.json()["id"])


async def test_an_applied_catalog_is_live_in_trino(applying_client: AsyncClient) -> None:
    await applying_client.post("/api/v1/catalogs", json=MEMORY)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    catalogs = (await applying_client.get("/api/v1/catalogs")).json()
    assert [c["name"] for c in catalogs] == ["scratch"]


async def test_a_successful_apply_commits_a_snapshot(applying_client: AsyncClient) -> None:
    await applying_client.post("/api/v1/catalogs", json=MEMORY)

    record = await _apply(applying_client)

    assert record["snapshot"] == 1
    snapshot = (await applying_client.get("/api/v1/snapshots/1")).json()
    assert list(snapshot["sections"]["catalogs"]) == ["scratch"]
    assert snapshot["apply_id"] == record["id"]


async def test_snapshots_are_numbered_sequentially(applying_client: AsyncClient) -> None:
    await applying_client.post("/api/v1/catalogs", json=MEMORY)
    await _apply(applying_client)
    await applying_client.post("/api/v1/catalogs", json={**MEMORY, "name": "second"})
    await _apply(applying_client)

    numbers = [s["number"] for s in (await applying_client.get("/api/v1/snapshots")).json()]

    assert numbers == [2, 1]


async def test_the_secret_is_patched_before_the_ddl(
    applying_client: AsyncClient, fake_kubernetes
) -> None:
    """The durable record exists before the live change, so a failed DDL leaves
    something recoverable rather than a catalog that vanishes at the next restart."""
    await applying_client.post("/api/v1/catalogs", json=MEMORY)

    await _apply(applying_client)

    assert fake_kubernetes.secrets[SECRET]["scratch.properties"] == "connector.name=memory\n"


async def test_the_candidate_is_re_derived_after_a_successful_apply(
    applying_client: AsyncClient,
) -> None:
    await applying_client.post("/api/v1/catalogs", json=MEMORY)
    await _apply(applying_client)

    review = (await applying_client.get("/api/v1/review")).json()

    assert review["base_snapshot"] == 1
    assert review["has_changes"] is False


async def test_review_diffs_against_the_committed_snapshot(applying_client: AsyncClient) -> None:
    """Review answers "what would this Apply change", not "what is staged"."""
    await applying_client.post("/api/v1/catalogs", json=MEMORY)
    await _apply(applying_client)

    await applying_client.post("/api/v1/catalogs", json={**MEMORY, "name": "later"})

    changes = (await applying_client.get("/api/v1/review")).json()["sections"][0]["changes"]
    assert [(c["resource"], c["change"]) for c in changes] == [("later", "added")]


async def test_a_dropped_catalog_leaves_trino_and_the_secret(
    applying_client: AsyncClient, fake_kubernetes
) -> None:
    """Removing a Catalog must remove it from the durable copy too, or it would
    return at the next pod restart."""
    await applying_client.post("/api/v1/catalogs", json=MEMORY)
    await _apply(applying_client)

    await applying_client.delete("/api/v1/catalogs/scratch")
    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert "scratch.properties" not in fake_kubernetes.secrets[SECRET]
    assert (await applying_client.get("/api/v1/catalogs")).json() == []


async def test_an_unknown_snapshot_is_not_found(applying_client: AsyncClient) -> None:
    missing = await applying_client.get("/api/v1/snapshots/99")

    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"

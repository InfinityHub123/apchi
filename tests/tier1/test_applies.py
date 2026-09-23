"""Apply as a resource: the state machine, the stream, and surviving a crash.

What is under test is the machinery driving an Apply, not what an Apply does. The
tests that need one to reach `succeeded` take the real coordinator, because
Verification queries it.
"""

import asyncio
import json

from httpx import AsyncClient

from app.pipeline.applies import TERMINAL

PG = {
    "name": "finance",
    "connector": "postgresql",
    "properties": {"connection-url": "jdbc:postgresql://db:5432/f"},
}


async def _settled(client: AsyncClient, apply_id: str, timeout: float = 5.0) -> dict:
    """Polls until the Apply reaches a terminal stage."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.02)
    raise AssertionError(f"Apply {apply_id} never settled; last stage {record['stage']!r}")


async def test_apply_returns_immediately_with_an_identifier(client: AsyncClient) -> None:
    started = await client.post("/api/v1/applies")

    assert started.status_code == 202
    assert started.json()["id"].startswith("apl_")
    assert started.json()["stage"] == "validating"


async def test_an_apply_walks_every_stage_in_order(applying_client: AsyncClient) -> None:
    apply_id = (await applying_client.post("/api/v1/applies")).json()["id"]

    record = await _settled(applying_client, apply_id)

    assert record["stage"] == "succeeded"
    assert [event["stage"] for event in record["history"]] == [
        "validating",
        "applying",
        "verifying",
        "committing",
        "succeeded",
    ]


async def test_the_record_holds_the_full_history_not_just_the_current_stage(
    applying_client: AsyncClient,
) -> None:
    """So a client reconnecting mid-Apply can replay rather than see a blank timeline."""
    apply_id = (await applying_client.post("/api/v1/applies")).json()["id"]

    record = await _settled(applying_client, apply_id)

    assert len(record["history"]) == 5
    assert all(event["at"] for event in record["history"])


async def test_applies_are_listed_newest_first(applying_client: AsyncClient) -> None:
    """Takes the real coordinator: an Apply that cannot verify ends in an incident,
    and an incident engages Maintenance Mode, which refuses the second Apply."""
    first = (await applying_client.post("/api/v1/applies")).json()["id"]
    await _settled(applying_client, first)
    second = (await applying_client.post("/api/v1/applies")).json()["id"]
    await _settled(applying_client, second)

    listed = [record["id"] for record in (await applying_client.get("/api/v1/applies")).json()]

    assert listed[:2] == [second, first]


async def test_an_unknown_apply_is_not_found(client: AsyncClient) -> None:
    missing = await client.get("/api/v1/applies/apl_nope")

    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


async def test_the_stream_replays_stages_already_past(applying_client: AsyncClient) -> None:
    """Connecting after the Apply finished still yields every stage."""
    apply_id = (await applying_client.post("/api/v1/applies")).json()["id"]
    await _settled(applying_client, apply_id)

    async with applying_client.stream("GET", f"/api/v1/applies/{apply_id}/events") as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    stages = [
        json.loads(line[6:])["stage"] for line in body.splitlines() if line.startswith("data: ")
    ]
    assert stages == ["validating", "applying", "verifying", "committing", "succeeded"]


async def test_a_reconnect_replays_only_what_was_missed(applying_client: AsyncClient) -> None:
    """Last-Event-ID is what makes a dropped connection cheap."""
    apply_id = (await applying_client.post("/api/v1/applies")).json()["id"]
    await _settled(applying_client, apply_id)

    async with applying_client.stream(
        "GET", f"/api/v1/applies/{apply_id}/events", headers={"Last-Event-ID": "2"}
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    stages = [
        json.loads(line[6:])["stage"] for line in body.splitlines() if line.startswith("data: ")
    ]
    assert stages == ["committing", "succeeded"]


async def test_events_carry_ids_so_a_client_can_resume(applying_client: AsyncClient) -> None:
    apply_id = (await applying_client.post("/api/v1/applies")).json()["id"]
    await _settled(applying_client, apply_id)

    async with applying_client.stream("GET", f"/api/v1/applies/{apply_id}/events") as response:
        body = "".join([chunk async for chunk in response.aiter_text()])

    assert [line for line in body.splitlines() if line.startswith("id: ")] == [
        f"id: {index}" for index in range(5)
    ]

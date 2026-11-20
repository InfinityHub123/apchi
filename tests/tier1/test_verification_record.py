"""Which of Verification's checks ran, and how each came out.

Verification is the only thing in Apchi that decides a Cluster is healthy, and before this an
Apply recorded only that it passed or why it did not -- one sentence. The failure cases live
in `test_auto_rollback.py`, beside the fixture that forces one; these are the other two
shapes, and the invariant that keeps the list complete.
"""

import asyncio

from httpx import AsyncClient

from app.pipeline.applies import TERMINAL
from app.pipeline.verification import check_order
from app.sections.registry import REGISTERED
from tests.conftest import FakeKubernetes

CATALOG = {"name": "scratch", "connector": "memory", "properties": {}}
LISTENER = {
    "name": "audit",
    "type": "http",
    "properties": {"http-event-listener.connect-ingest-uri": "http://collector:8080/e"},
}


async def _applied(client: AsyncClient) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    record: dict = {}
    for _ in range(1200):
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError(f"Apply never settled: {record}")


def test_every_section_has_an_adoption_check() -> None:
    """Derived from the registry rather than listed, so a Section added later gets a check
    in the record without anybody remembering to add one here."""
    order = check_order()

    assert order[:3] == ("coordinator_responding", "workers_registered", "smoke_query")
    assert order[3:] == tuple(f"{section.name}_adopted" for section in REGISTERED)


async def test_a_successful_apply_records_every_check_as_passed(
    applying_client: AsyncClient,
) -> None:
    await applying_client.post("/api/v1/catalogs", json=CATALOG)

    record = await _applied(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert [check["check"] for check in record["verification"]] == list(check_order())
    assert {check["outcome"] for check in record["verification"]} == {"passed"}


async def test_a_passed_check_says_what_it_found(applying_client: AsyncClient) -> None:
    """A single-node Trino is its own coordinator, so the honest count is 0 of 0."""
    await applying_client.post("/api/v1/catalogs", json=CATALOG)

    record = await _applied(applying_client)

    workers = next(c for c in record["verification"] if c["check"] == "workers_registered")
    assert workers["detail"] == "0 of 0 workers registered."


async def test_an_apply_that_never_reached_verification_records_no_checks(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Empty, not a list of `not_reached`. A Validation failure means nothing was delivered
    and nothing was checked, which is a different statement from "Verification started and
    stopped" -- the same distinction `last_apply.verified` draws with null."""
    fake_kubernetes.pod_problem = "the validation pod failed"
    await applying_client.post("/api/v1/event-listeners", json=LISTENER)

    record = await _applied(applying_client)

    assert record["stage"] == "failed"
    assert record["verification"] == []

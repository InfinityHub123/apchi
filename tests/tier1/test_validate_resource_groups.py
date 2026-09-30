"""Validating and applying Resource Groups.

Two files, one Section: the rules and the `resource-groups.properties` that makes Trino read
them. They arrive together and they leave together, which is most of what is worth asserting
here -- leaving the properties file behind would point Trino at a file that is no longer
mounted, and Trino refuses to start on that.

The other half is the cross-resource checks. A selector may name a group staged later in the
same session, so "does this group exist" is a question about the whole Candidate and belongs
at Validate rather than at request time.
"""

import asyncio
import json

from httpx import AsyncClient

from app.pipeline.applies import TERMINAL
from app.pipeline.impact import RESTART_WARNING
from app.sections.resource_groups.generator import (
    MANAGER_KEY,
    MANAGER_PATH,
    RULES_KEY,
    RULES_PATH,
)
from tests.conftest import FakeKubernetes

SECRET = "trino-resource-groups"
VOLUME = "apchi-resource-groups"
GLOBAL = {"path": "global", "hard_concurrency_limit": 100, "max_queued": 1000}
ETL = {"path": "global.etl", "hard_concurrency_limit": 10}


async def _validation(client: AsyncClient, timeout: float = 90.0) -> dict:
    started = await client.post("/api/v1/validations")
    assert started.status_code == 202
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/validations/{started.json()['id']}")).json()
        if record["outcome"] != "running":
            return record
        await asyncio.sleep(0.05)
    raise AssertionError("Validation never finished")


async def _apply(client: AsyncClient, timeout: float = 180.0) -> dict:
    started = await client.post("/api/v1/applies")
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError("Apply never settled")


def _rules(kubernetes: FakeKubernetes) -> dict:
    return json.loads(kubernetes.secrets[SECRET][RULES_KEY])


async def test_a_selector_naming_a_group_that_does_not_exist_fails_validation(
    applying_client: AsyncClient,
) -> None:
    """Trino catches this too, at the cost of a probe that will not start. Apchi catches it
    without one, and says which selector."""
    await applying_client.post("/api/v1/resource-groups", json=GLOBAL)
    await applying_client.put(
        "/api/v1/resource-groups/selectors", json={"selectors": [{"group": "global.missing"}]}
    )

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "failed"
    failure = verdict["failures"][0]
    assert failure["section"] == "resource_groups"
    assert failure["resource"] == "#selectors[0]"
    assert "global.missing" in failure["reason"]


async def test_a_selector_naming_a_group_with_subgroups_fails_validation(
    applying_client: AsyncClient,
) -> None:
    """Trino starts happily on this one and fails the queries instead, which is exactly the
    kind of failure Validation exists to move earlier."""
    await applying_client.post("/api/v1/resource-groups", json=GLOBAL)
    await applying_client.post("/api/v1/resource-groups", json=ETL)
    await applying_client.put(
        "/api/v1/resource-groups/selectors", json={"selectors": [{"group": "global"}]}
    )

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "failed"
    assert "subgroups" in verdict["failures"][0]["reason"]


async def test_a_cpu_limit_without_a_quota_period_fails_validation(
    applying_client: AsyncClient,
) -> None:
    """A real coordinator refuses to start on this: "cpuQuotaPeriod must be specified to use
    CPU limits on group: etl"."""
    await applying_client.post("/api/v1/resource-groups", json=GLOBAL)
    await applying_client.post(
        "/api/v1/resource-groups",
        json={
            "path": "global.etl",
            "hard_concurrency_limit": 1,
            "soft_cpu_limit": "30m",
            "hard_cpu_limit": "1h",
        },
    )

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "failed"
    assert verdict["failures"][0]["resource"] == "global.etl"
    assert "CPU quota period" in verdict["failures"][0]["reason"]


async def test_a_quota_period_makes_cpu_limits_acceptable(applying_client: AsyncClient) -> None:
    await applying_client.put("/api/v1/resource-groups/settings", json={"cpu_quota_period": "1h"})
    await applying_client.post(
        "/api/v1/resource-groups",
        json={
            "path": "global",
            "hard_concurrency_limit": 1,
            "soft_cpu_limit": "30m",
            "hard_cpu_limit": "1h",
        },
    )

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "passed", verdict["failures"]


async def test_the_probe_is_started_with_both_files_in_place(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The JSON is inert without the properties file, so a probe holding only the rules
    would start on any rules at all."""
    await applying_client.post("/api/v1/resource-groups", json=GLOBAL)
    await applying_client.put(
        "/api/v1/resource-groups/selectors", json={"selectors": [{"group": "global"}]}
    )

    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "passed", verdict["failures"]
    pod = fake_kubernetes.pod_manifests[fake_kubernetes.pod_history[-1]]
    mounts = {
        m["mountPath"]: m.get("subPath") for m in pod["spec"]["containers"][0]["volumeMounts"]
    }
    assert mounts[RULES_PATH] == RULES_KEY
    assert mounts[MANAGER_PATH] == MANAGER_KEY
    delivered = fake_kubernetes.secret_history[fake_kubernetes.pod_history[-1]]
    assert RULES_PATH in delivered[MANAGER_KEY], "the properties file points at the rules"


async def test_an_empty_section_needs_no_probe(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    verdict = await _validation(applying_client)

    assert verdict["outcome"] == "passed"
    assert fake_kubernetes.pod_history == []


async def test_applying_delivers_both_files_and_restarts_the_coordinator(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/resource-groups", json=GLOBAL)
    await applying_client.post("/api/v1/resource-groups", json=ETL)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    rules = _rules(fake_kubernetes)
    assert [group["name"] for group in rules["rootGroups"]] == ["global"]
    assert [group["name"] for group in rules["rootGroups"][0]["subGroups"]] == ["etl"]
    assert rules["selectors"] == []
    assert set(fake_kubernetes.secrets[SECRET]) == {RULES_KEY, MANAGER_KEY}
    assert {RULES_PATH, MANAGER_PATH} <= set(fake_kubernetes.mounts)
    assert fake_kubernetes.mounts[RULES_PATH]["volume"] == VOLUME
    assert len(fake_kubernetes.restarts) == 1
    assert "resource_groups" in fake_kubernetes.restarts[0]


async def test_emptying_the_section_takes_both_files_away(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Unmounting only the rules would leave Trino pointed at a file that is not there, and
    it refuses to start on that."""
    await applying_client.post("/api/v1/resource-groups", json=GLOBAL)
    await _apply(applying_client)
    await applying_client.delete("/api/v1/resource-groups/global")

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    assert fake_kubernetes.secrets[SECRET] == {}
    assert not {RULES_PATH, MANAGER_PATH} & set(fake_kubernetes.mounts)


async def test_reverting_the_groups_carries_the_restart_warning(
    applying_client: AsyncClient,
) -> None:
    await applying_client.post("/api/v1/resource-groups", json=GLOBAL)
    await _apply(applying_client)
    await applying_client.post("/api/v1/resource-groups", json=ETL)
    await _apply(applying_client)

    effect = (
        await applying_client.post("/api/v1/resource-groups/revert", json={"snapshot": 1})
    ).json()

    assert effect["sections"] == ["resource_groups"]
    assert effect["cost"]["restarts_coordinator"] is True
    assert RESTART_WARNING in effect["summary"]
    assert [g["path"] for g in (await applying_client.get("/api/v1/resource-groups")).json()] == [
        "global"
    ]

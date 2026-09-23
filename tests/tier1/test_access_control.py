"""The generated access-control file, and the deployment preconditions around it.

Only Apchi may create or drop Catalogs. That restriction is a file Apchi writes, so
what matters here is that the file says the right thing, that Apply delivers it, and
that Apchi refuses to apply against a deployment where delivering it would silently
achieve nothing.
"""

import asyncio
import json

import pytest
from httpx import AsyncClient

from app.config import Settings
from app.pipeline.access_control import RULES_KEY, render_rules
from app.pipeline.applies import TERMINAL
from app.pipeline.preconditions import PreconditionFailed, check, pod_spec
from tests.conftest import FakeKubernetes, healthy_pod_spec

ACCESS_CONTROL_SECRET = "trino-access-control"
MEMORY = {"name": "scratch", "connector": "memory", "properties": {}}


async def _apply(client: AsyncClient, timeout: float = 60.0) -> dict:
    started = await client.post("/api/v1/applies")
    assert started.status_code == 202, started.json()
    apply_id = started.json()["id"]
    deadline = asyncio.get_running_loop().time() + timeout
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError(f"Apply never settled; last={record.get('stage')}")


def test_apchi_gets_owner_and_everyone_else_gets_all() -> None:
    """checkCanCreateCatalog gates on the owner access mode, so owner is the whole
    point; `all` is full access to existing catalogs without it."""
    rules = json.loads(render_rules("apchi"))

    assert rules["catalogs"] == [
        {"user": "^apchi$", "allow": "owner"},
        {"allow": "all"},
    ]


def test_the_catch_all_rule_is_present() -> None:
    """canAccessCatalog returns false when nothing matches, so omitting the catch-all
    denies every End User access to every catalog."""
    rules = json.loads(render_rules("apchi"))

    catch_all = rules["catalogs"][-1]
    assert catch_all == {"allow": "all"}, "the last rule must match every identity"


def test_apchis_rule_comes_first() -> None:
    """First match wins. Behind the catch-all, Apchi would get `all` and lose the owner
    mode that CREATE CATALOG needs."""
    rules = json.loads(render_rules("apchi"))

    assert "user" in rules["catalogs"][0]


def test_the_identity_is_anchored_so_a_lookalike_does_not_match() -> None:
    """A bare `apchi` already cannot match `notapchi`, but the cost of being wrong is
    handing catalog DDL to anyone whose username contains Apchi's."""
    rules = json.loads(render_rules("apchi"))

    assert rules["catalogs"][0]["user"] == "^apchi$"


async def test_an_apply_delivers_the_rules(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.post("/api/v1/catalogs", json=MEMORY)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    delivered = fake_kubernetes.secrets[ACCESS_CONTROL_SECRET]
    assert json.loads(delivered[RULES_KEY])["catalogs"][0]["allow"] == "owner"


async def test_rules_changed_outside_apchi_are_corrected_by_the_next_apply(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Delivered every Apply rather than written once, so a Cluster whose rules were
    edited behind Apchi's back does not quietly keep catalog DDL open to everyone."""
    await fake_kubernetes.write_secret(ACCESS_CONTROL_SECRET, {RULES_KEY: '{"catalogs": []}'})

    await _apply(applying_client)

    assert json.loads(fake_kubernetes.secrets[ACCESS_CONTROL_SECRET][RULES_KEY]) == json.loads(
        render_rules("apchi")
    )


def test_the_dev_deployment_meets_every_precondition(settings: Settings) -> None:
    check(pod_spec(healthy_pod_spec()), settings)


def test_a_subpath_mount_of_an_apchi_managed_secret_is_refused(settings: Settings) -> None:
    """A subPath mount never receives updates. Apchi would write the file, the write
    would succeed, and Trino would never see the change."""
    spec = healthy_pod_spec()
    spec["containers"][0]["volumeMounts"][1] = {
        "name": "access-control",
        "mountPath": "/etc/trino/access-control/rules.json",
        "subPath": RULES_KEY,
    }

    with pytest.raises(PreconditionFailed) as raised:
        check(pod_spec(spec), settings)

    assert "subPath" in str(raised.value)
    assert "trino-access-control" in str(raised.value)


def test_a_missing_seed_init_container_is_refused(settings: Settings) -> None:
    """Without it the coordinator starts with an empty store and every catalog in the
    latest Snapshot is missing."""
    spec = healthy_pod_spec()
    spec["initContainers"] = []

    with pytest.raises(PreconditionFailed) as raised:
        check(pod_spec(spec), settings)

    assert "initContainer" in str(raised.value)


def test_a_read_only_volume_over_the_catalog_store_is_refused(settings: Settings) -> None:
    """Trino writes that directory itself. A Secret mount there is read-only, which is
    the finding the whole seed design rests on."""
    spec = healthy_pod_spec()
    spec["volumes"] = [
        v
        if v["name"] != "catalog-store"
        else {"name": "catalog-store", "secret": {"secretName": "x"}}
        for v in spec["volumes"]
    ]

    with pytest.raises(PreconditionFailed) as raised:
        check(pod_spec(spec), settings)

    assert "CREATE CATALOG would fail" in str(raised.value)


def test_a_read_only_volume_above_the_catalog_store_is_refused(settings: Settings) -> None:
    """Mounting over the parent breaks it just as thoroughly."""
    spec = healthy_pod_spec()
    spec["containers"][0]["volumeMounts"].append(
        {"name": "catalog-seed", "mountPath": "/data/trino"}
    )

    with pytest.raises(PreconditionFailed) as raised:
        check(pod_spec(spec), settings)

    assert "/data/trino" in str(raised.value)


def test_an_empty_dir_over_the_catalog_store_is_accepted(settings: Settings) -> None:
    """The positive case, because it is the one the design actually asks for: an
    emptyDir there is writable and is what the initContainer seeds."""
    check(pod_spec(healthy_pod_spec()), settings)


def test_every_problem_is_reported_not_just_the_first(settings: Settings) -> None:
    """An Admin fixing a manifest should see the whole list."""
    spec = healthy_pod_spec()
    spec["initContainers"] = []
    spec["containers"][0]["volumeMounts"][1] = {
        "name": "access-control",
        "mountPath": "/etc/trino/access-control/rules.json",
        "subPath": RULES_KEY,
    }

    with pytest.raises(PreconditionFailed) as raised:
        check(pod_spec(spec), settings)

    assert len(raised.value.problems) == 2


async def test_an_apply_is_refused_when_a_precondition_is_broken(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, trino_cluster
) -> None:
    """Checked before every Apply, because a chart change can reintroduce a violation
    silently. It fails at Validation, so nothing reaches the Cluster."""
    fake_kubernetes.pod_spec = healthy_pod_spec()
    fake_kubernetes.pod_spec["initContainers"] = []
    await applying_client.post("/api/v1/catalogs", json=MEMORY)

    record = await _apply(applying_client)

    assert record["stage"] == "failed"
    assert "PreconditionFailed" in record["failure_reason"]
    assert record["rollback"] is None, "nothing was touched, so there is nothing to undo"

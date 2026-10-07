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
from app.pipeline.applies import TERMINAL
from app.pipeline.preconditions import PreconditionFailed, check, pod_spec
from app.sections.certificate_mapping.generator import MOUNT_PATH as MAPPING_PATH
from app.sections.permissions.generator import MOUNT_PATH, RULES_KEY, render_rules
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


def _remount(spec: dict, path: str, mount: dict) -> dict:
    """Replace the mount at a path, rather than at an index.

    By position was how this used to be written, and it broke the moment the fixture grew a
    mount: the test went on editing slot 1 and started breaking something else, which showed
    up as an extra precondition failure rather than as a wrong test.
    """
    mounts = spec["containers"][0]["volumeMounts"]
    spec["containers"][0]["volumeMounts"] = [m for m in mounts if m["mountPath"] != path] + [mount]
    return spec


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


async def test_the_dev_deployment_meets_every_precondition(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)


async def test_a_subpath_mount_of_an_apchi_managed_secret_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """A subPath mount never receives updates. Apchi would write the file, the write
    would succeed, and Trino would never see the change."""
    spec = _remount(
        healthy_pod_spec(),
        "/etc/trino/access-control",
        {
            "name": "access-control",
            "mountPath": "/etc/trino/access-control/rules.json",
            "subPath": RULES_KEY,
        },
    )

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(spec), settings)

    assert "subPath" in str(raised.value)
    assert "trino-access-control" in str(raised.value)


async def test_a_missing_seed_init_container_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Without it the coordinator starts with an empty store and every catalog in the
    latest Snapshot is missing."""
    spec = healthy_pod_spec()
    spec["initContainers"] = []

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(spec), settings)

    assert "initContainer" in str(raised.value)


async def test_a_read_only_volume_over_the_catalog_store_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
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
        await check(fake_kubernetes, pod_spec(spec), settings)

    assert "CREATE CATALOG would fail" in str(raised.value)


async def test_a_read_only_volume_above_the_catalog_store_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Mounting over the parent breaks it just as thoroughly."""
    spec = healthy_pod_spec()
    spec["containers"][0]["volumeMounts"].append(
        {"name": "catalog-seed", "mountPath": "/data/trino"}
    )

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(spec), settings)

    assert "/data/trino" in str(raised.value)


async def test_an_empty_dir_over_the_catalog_store_is_accepted(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The positive case, because it is the one the design actually asks for: an
    emptyDir there is writable and is what the initContainer seeds."""
    await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)


async def test_every_problem_is_reported_not_just_the_first(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """An Admin fixing a manifest should see the whole list."""
    spec = _remount(
        healthy_pod_spec(),
        "/etc/trino/access-control",
        {
            "name": "access-control",
            "mountPath": "/etc/trino/access-control/rules.json",
            "subPath": RULES_KEY,
        },
    )
    spec["initContainers"] = []

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(spec), settings)

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


async def test_every_file_a_section_declares_is_guarded(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The precondition reads the registry rather than naming a path.

    This is the property worth having: a Section added later is guarded without anyone
    remembering to extend the check, and forgetting is the likely failure because nothing
    breaks when you do -- Apchi and an Admin would simply overwrite each other's file, last
    writer winning, silently.
    """
    from app.pipeline.files import owned_paths
    from app.sections.registry import REGISTERED

    guarded = owned_paths(REGISTERED, settings)
    assert guarded, "no Section declares a file; this test has stopped proving anything"

    for path, volume in guarded.items():
        spec = healthy_pod_spec()
        spec["containers"][0]["volumeMounts"].append({"name": "someone-elses", "mountPath": path})

        with pytest.raises(PreconditionFailed) as raised:
            await check(fake_kubernetes, pod_spec(spec), settings)

        assert path in str(raised.value)
        assert volume in str(raised.value)


async def test_apchi_mounts_nothing_for_the_rules(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The rules have to be a whole-volume mount, which is the Admin's to make. Apchi owns
    what is in the Secret and nothing else, so delivering them touches no pod template."""
    await applying_client.post("/api/v1/catalogs", json=MEMORY)

    await _apply(applying_client)

    assert fake_kubernetes.secrets[ACCESS_CONTROL_SECRET] != {}
    assert MOUNT_PATH not in fake_kubernetes.mounts
    assert fake_kubernetes.restarts == [], "a permission change costs no queries"


async def test_the_rules_are_delivered_even_though_nothing_is_staged(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """A Section with nothing in the Candidate still has a file. Apchi generates the whole
    of this one, so "empty" is not a reason to leave the Cluster without it."""
    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert fake_kubernetes.secrets[ACCESS_CONTROL_SECRET][RULES_KEY] == render_rules("apchi")


async def test_permissions_appears_in_review_and_costs_no_restart(client: AsyncClient) -> None:
    review = (await client.get("/api/v1/review")).json()

    permissions = next(s for s in review["sections"] if s["section"] == "permissions")
    assert permissions["changes"] == []
    assert review["cost"]["restarts_coordinator"] is False


async def test_a_subpath_mount_of_a_file_apchi_mounts_itself_is_allowed(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The precondition must not over-reach. Apchi mounts single keys with subPath on
    purpose -- the user-mapping file is one -- and replaces the mount when the file changes,
    with a Rollout making the new content live. Rejecting those would refuse every
    deployment Apchi itself produces."""
    spec = healthy_pod_spec()
    mounts = [m["mountPath"] for m in spec["containers"][0]["volumeMounts"]]

    assert MAPPING_PATH in mounts, "the fixture mounts it the way Apchi does"
    await check(fake_kubernetes, pod_spec(spec), settings)


async def test_a_cluster_that_never_rereads_the_rules_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Without `security.refresh-period` Trino reads the rules once at startup and never
    again, so Apchi would write a permission change, report success, and the Cluster would
    never see it. The one precondition that reads the Admin's configuration rather than
    their pod template."""
    fake_kubernetes.config_maps["trino-config"]["access-control.properties"] = (
        "access-control.name=file\nsecurity.config-file=/etc/trino/access-control/rules.json\n"
    )

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)

    assert "security.refresh-period" in str(raised.value)
    assert "never see it" in str(raised.value)


async def test_a_cluster_told_to_read_no_rules_at_all_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Every permission Apchi applies would be written and ignored."""
    spec = healthy_pod_spec()
    spec["containers"][0]["volumeMounts"] = [
        mount
        for mount in spec["containers"][0]["volumeMounts"]
        if mount["mountPath"] != "/etc/trino/access-control.properties"
    ]

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(spec), settings)

    assert "written and ignored" in str(raised.value)


async def test_properties_apchi_cannot_read_are_reported_rather_than_assumed(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """A mount Apchi cannot follow is not the same as a missing property, and saying "no
    refresh period" would send an Admin looking for the wrong thing."""
    fake_kubernetes.config_maps.pop("trino-config")

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(healthy_pod_spec()), settings)

    assert "cannot read what is in it" in str(raised.value)


async def test_an_apply_is_refused_when_the_rules_would_never_be_reread(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Checked before every Apply, so a chart change that drops the refresh period stops
    Apchi rather than making it lie about what it applied."""
    fake_kubernetes.config_maps["trino-config"]["access-control.properties"] = (
        "access-control.name=file\n"
    )

    record = await _apply(applying_client)

    assert record["stage"] == "failed"
    assert "security.refresh-period" in (record["failure_reason"] or "")


async def test_the_whole_configuration_directory_counts_as_mounting_the_properties(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """How the official Trino chart does it, and how most charts do it: one ConfigMap
    mounted at /etc/trino rather than one mount per file. Apchi used to look only for the
    exact path, so it refused the official chart for a file that was there all along."""
    spec = healthy_pod_spec()
    spec["containers"][0]["volumeMounts"] = [
        mount
        for mount in spec["containers"][0]["volumeMounts"]
        if mount["mountPath"] != "/etc/trino/access-control.properties"
    ]
    spec["containers"][0]["volumeMounts"].append({"name": "config", "mountPath": "/etc/trino"})

    await check(fake_kubernetes, pod_spec(spec), settings)


async def test_a_directory_mount_without_the_refresh_period_is_still_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The looser matching must not become a way to pass the check without the property."""
    fake_kubernetes.config_maps["trino-config"]["access-control.properties"] = (
        "access-control.name=file\n"
    )
    spec = healthy_pod_spec()
    spec["containers"][0]["volumeMounts"] = [
        mount
        for mount in spec["containers"][0]["volumeMounts"]
        if mount["mountPath"] != "/etc/trino/access-control.properties"
    ]
    spec["containers"][0]["volumeMounts"].append({"name": "config", "mountPath": "/etc/trino"})

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(spec), settings)

    assert "security.refresh-period" in str(raised.value)


async def test_a_file_mount_overrides_the_directory_it_sits_in(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """A deployment doing both is serving the overlay, and so is Trino -- so the more
    specific mount is the one Apchi must read. Here the directory's copy would pass and
    the overlay's would not."""
    fake_kubernetes.config_maps["trino-overlay"] = {
        "access-control.properties": "access-control.name=file\n"
    }
    spec = healthy_pod_spec()
    spec["containers"][0]["volumeMounts"] = [
        mount
        for mount in spec["containers"][0]["volumeMounts"]
        if mount["mountPath"] != "/etc/trino/access-control.properties"
    ]
    spec["containers"][0]["volumeMounts"] += [
        {"name": "config", "mountPath": "/etc/trino"},
        {"name": "overlay", "mountPath": "/etc/trino/access-control.properties"},
    ]
    spec["volumes"].append({"name": "overlay", "configMap": {"name": "trino-overlay"}})

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(spec), settings)

    assert "security.refresh-period" in str(raised.value)


async def test_neither_the_file_nor_its_directory_mounted_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    spec = healthy_pod_spec()
    spec["containers"][0]["volumeMounts"] = [
        mount
        for mount in spec["containers"][0]["volumeMounts"]
        if mount["mountPath"] != "/etc/trino/access-control.properties"
    ]

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(spec), settings)

    assert "written and ignored" in str(raised.value)
    assert "/etc/trino" in str(raised.value)

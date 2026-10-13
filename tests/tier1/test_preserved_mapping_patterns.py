"""Preserving a Cluster's existing mapping patterns while it migrates onto Apchi.

The first Admin values: applied through the pipeline, never recorded in a Snapshot
(invariant 9, section 14). Two things follow from that and are what these tests are about --
the file is the Operator's pattern *merged with* the Admin's, and an Apply that carries only
an Admin change creates no Snapshot.
"""

import asyncio

from httpx import AsyncClient
from pymongo import AsyncMongoClient

from app.audit.events import AuditStore
from app.config import Settings
from app.pipeline.admin_values import AdminStore
from app.pipeline.applies import TERMINAL
from app.pipeline.impact import RESTART_WARNING
from app.sections.certificate_mapping.generator import FILE_KEY
from tests.conftest import MAPPING_SECRET, FakeKubernetes

PATTERN = {"pattern": "(.*)@example\\.com", "user": "$1"}
LEGACY = {"pattern": "CN=(.*?),.*", "user": "$1"}
OLDER = {"pattern": "(.*)\\.clients\\.internal", "user": "$1", "case": "lower"}
CATALOG = {"name": "scratch", "connector": "memory", "properties": {}}


async def _apply(
    client: AsyncClient, path: str = "/api/v1/applies", timeout: float = 180.0
) -> dict:
    started = await client.post(path)
    assert started.status_code == 202, started.json()
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError("Apply never settled")


def _rules(kubernetes: FakeKubernetes) -> list[dict]:
    import json

    return json.loads(kubernetes.secrets[MAPPING_SECRET][FILE_KEY])["rules"]


async def test_no_patterns_are_preserved_to_begin_with(client: AsyncClient) -> None:
    listed = await client.get("/api/v1/admin/certificate-mapping/preserved")

    assert listed.status_code == 200
    assert listed.json() == {"patterns": []}


async def test_an_admin_replaces_the_whole_list(client: AsyncClient) -> None:
    saved = await client.put(
        "/api/v1/admin/certificate-mapping/preserved", json={"patterns": [LEGACY, OLDER]}
    )

    assert saved.status_code == 200
    assert [p["pattern"] for p in saved.json()["patterns"]] == [LEGACY["pattern"], OLDER["pattern"]]
    listed = (await client.get("/api/v1/admin/certificate-mapping/preserved")).json()
    assert listed["patterns"][1]["case"] == "lower"


async def test_a_malformed_preserved_pattern_is_refused_naming_which_one(
    client: AsyncClient,
) -> None:
    """With several of them, "the pattern is invalid" does not say which pattern."""
    refused = await client.put(
        "/api/v1/admin/certificate-mapping/preserved",
        json={"patterns": [LEGACY, {"pattern": "(unclosed"}]},
    )

    assert refused.status_code == 422
    assert [d["property"] for d in refused.json()["details"]] == ["patterns[1].pattern"]


async def test_preserved_patterns_stage_nothing(client: AsyncClient) -> None:
    """They are not Operator configuration, so they do not show up as staged changes."""
    await client.put("/api/v1/admin/certificate-mapping/preserved", json={"patterns": [LEGACY]})

    review = (await client.get("/api/v1/review")).json()

    mapping = next(s for s in review["sections"] if s["section"] == "certificate_mapping")
    assert mapping["changes"] == []
    assert review["has_changes"] is False


async def test_review_still_says_applying_will_restart_the_coordinator(
    applying_client: AsyncClient,
) -> None:
    """Nothing is staged, but the file the Cluster holds is no longer the file Apchi would
    write -- and adopting that costs a restart like any other mapping change."""
    await applying_client.put(
        "/api/v1/admin/certificate-mapping/preserved", json={"patterns": [LEGACY]}
    )

    review = (await applying_client.get("/api/v1/review")).json()

    assert review["has_changes"] is False
    assert review["cost"]["restarts_coordinator"] is True
    assert review["cost"]["warning"] == RESTART_WARNING


async def test_the_operators_pattern_comes_first_and_the_preserved_ones_beneath(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """First match wins, so a subject matching both resolves to the convention being
    migrated *to* rather than the one being migrated from."""
    await applying_client.put("/api/v1/certificate-mapping", json=PATTERN)
    await applying_client.put(
        "/api/v1/admin/certificate-mapping/preserved", json={"patterns": [LEGACY, OLDER]}
    )

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    assert [rule["pattern"] for rule in _rules(fake_kubernetes)] == [
        "^apchi$",
        PATTERN["pattern"],
        LEGACY["pattern"],
        OLDER["pattern"],
        "(.*)",
    ]


async def test_an_admin_apply_delivers_them_without_creating_a_snapshot(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Snapshots are the history of Operator configuration, and this changed none of it."""
    await applying_client.post("/api/v1/catalogs", json=CATALOG)
    await _apply(applying_client)
    before = (await applying_client.get("/api/v1/snapshots")).json()["items"]

    await applying_client.put(
        "/api/v1/admin/certificate-mapping/preserved", json={"patterns": [LEGACY]}
    )
    record = await _apply(applying_client, "/api/v1/admin/applies")

    assert record["stage"] == "succeeded", record
    assert record["snapshot"] is None
    assert (await applying_client.get("/api/v1/snapshots")).json()["items"] == before
    assert LEGACY["pattern"] in [rule["pattern"] for rule in _rules(fake_kubernetes)]


async def test_an_admin_apply_leaves_a_staged_candidate_alone(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """An Admin Apply must not push an Operator's unreviewed changes to the Cluster."""
    await applying_client.put(
        "/api/v1/admin/certificate-mapping/preserved", json={"patterns": [LEGACY]}
    )
    await applying_client.post("/api/v1/catalogs", json=CATALOG)

    record = await _apply(applying_client, "/api/v1/admin/applies")

    assert record["stage"] == "succeeded", record
    assert "scratch" not in fake_kubernetes.secrets.get("trino-catalog-seed", {})
    staged = (await applying_client.get("/api/v1/catalogs")).json()
    assert [c["name"] for c in staged] == ["scratch"], "still staged, and still only staged"


async def test_removing_the_last_preserved_pattern_leaves_only_the_operators(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await applying_client.put("/api/v1/certificate-mapping", json=PATTERN)
    await applying_client.put(
        "/api/v1/admin/certificate-mapping/preserved", json={"patterns": [LEGACY]}
    )
    await _apply(applying_client)

    await applying_client.put("/api/v1/admin/certificate-mapping/preserved", json={"patterns": []})
    record = await _apply(applying_client, "/api/v1/admin/applies")

    assert record["stage"] == "succeeded", record
    assert [rule["pattern"] for rule in _rules(fake_kubernetes)] == [
        "^apchi$",
        PATTERN["pattern"],
        "(.*)",
    ]


async def test_preserved_patterns_survive_a_full_rollback(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Invariant 2: Admin values deliberately survive a rollback. They are platform state,
    and an Operator's rollback should not revert them."""
    await applying_client.put(
        "/api/v1/admin/certificate-mapping/preserved", json={"patterns": [LEGACY]}
    )
    await applying_client.put("/api/v1/certificate-mapping", json=PATTERN)
    await _apply(applying_client)

    await applying_client.post("/api/v1/candidate/rollback", json={"snapshot": 1})
    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    patterns = [rule["pattern"] for rule in _rules(fake_kubernetes)]
    assert LEGACY["pattern"] in patterns
    assert (await applying_client.get("/api/v1/admin/certificate-mapping/preserved")).json()[
        "patterns"
    ] != []


async def test_a_cluster_mid_migration_may_have_no_operator_pattern_at_all(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Onboarding order is the Admin's: preserve first, choose the destination later."""
    await applying_client.put(
        "/api/v1/admin/certificate-mapping/preserved", json={"patterns": [LEGACY]}
    )

    record = await _apply(applying_client, "/api/v1/admin/applies")

    assert record["stage"] == "succeeded", record
    assert [rule["pattern"] for rule in _rules(fake_kubernetes)] == [
        "^apchi$",
        LEGACY["pattern"],
        "(.*)",
    ]


async def test_an_admin_apply_is_refused_while_another_apply_is_in_flight(
    client: AsyncClient,
) -> None:
    """One Cluster, one Apply. The Candidate is frozen for an Admin Apply exactly as it is
    for an Operator's."""
    await client.post("/api/v1/applies")

    refused = await client.post("/api/v1/admin/applies")

    assert refused.status_code == 409


async def test_replacing_the_patterns_leaves_the_other_admin_values_alone(
    client: AsyncClient, settings: Settings
) -> None:
    """One Admin document, several unrelated Admin decisions in it.

    The store replaces the whole document, which is the right contract for a list an Admin
    sends whole. It is the wrong thing to do with the fields the request never mentioned:
    building `AdminValues` from this request alone reset every one of them to its default.
    Two decisions live in there and both are consequential on a serving Cluster. The default
    for enforcement is off, so preserving a mapping pattern mid-migration opened the Cluster
    to every identity without a grant. And the default for the preserved access-control
    rules is none, so it also dropped the rules an adopted Cluster was already running --
    which only Adoption writes, which is why it is seeded here rather than sent.
    """
    assert (
        await client.put("/api/v1/admin/permissions/enforcement", json={"enforced": True})
    ).status_code == 200
    mongo: AsyncMongoClient = AsyncMongoClient(settings.mongo_uri)
    store = AdminStore(mongo[settings.mongo_database], AuditStore(mongo[settings.mongo_database]))
    rules = {"catalog": [{"user": "legacy_etl", "catalog": ".*", "allow": "all"}]}
    await store.save((await store.load()).model_copy(update={"preserved_access_control": rules}))

    replaced = await client.put(
        "/api/v1/admin/certificate-mapping/preserved", json={"patterns": [LEGACY]}
    )

    assert replaced.status_code == 200, replaced.json()
    assert replaced.json()["patterns"][0]["pattern"] == LEGACY["pattern"]
    try:
        kept = await store.load()
    finally:
        await mongo.close()
    assert kept.enforce_permissions is True, "the request said nothing about enforcement"
    assert kept.preserved_access_control == rules, (
        "nor about the rules the Cluster was running before Apchi adopted it"
    )

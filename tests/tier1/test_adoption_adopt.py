"""Adopting: the completed discovery becomes the Candidate, and Snapshot 1 follows.

Deliberately the smallest thing in Adoption. §15 is explicit that this is not a special case
of Commit -- the discovered configuration is validated, applied to the real Cluster and
verified, and the Snapshot it produces is a Snapshot like any other (invariant 2). So
`/validations` and `/applies` do the rest unchanged, and there is no second route to the
Cluster.

Which leaves the refusals as the substance. Each of the four protects something that would
otherwise fail quietly, and the ordering one is the whole reason your correction mattered: run
before the cutover, Verification has nothing to verify, and Snapshot 1 would be the
never-verified Snapshot §15 refuses to create.
"""

from httpx import AsyncClient
from testcontainers.core.container import DockerContainer

from app.adapters.trino import Trino
from tests.conftest import FakeKubernetes

BASE = "/api/v1/admin/adoption"
TERMINAL = {"succeeded", "failed", "incident"}


def _cluster(container: DockerContainer) -> Trino:
    return Trino(
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(8080)),
    )


async def _settled(client: AsyncClient, apply_id: str) -> dict:
    import asyncio

    deadline = asyncio.get_running_loop().time() + 180
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.05)
    raise AssertionError("the adoption never settled")


async def _ready_to_adopt(client: AsyncClient) -> None:
    """Discovered and seeded, on a Cluster whose deployment already mounts Apchi's Secrets.

    The healthy fixture is already cut over -- it mirrors deploy/trino-dev, which is what a
    Cluster looks like *after* the Admin's deployment change. So these tests exercise the
    step that follows the cutover, which is the only order in which it is meaningful.
    """
    created = await client.post(f"{BASE}/discoveries")
    assert created.status_code == 201, created.json()
    assert created.json()["complete"], created.json()["outstanding"]
    seeded = await client.post(f"{BASE}/seed")
    assert seeded.status_code == 200, seeded.json()


# --- the refusals -------------------------------------------------------------------


async def test_adopting_without_a_discovery_is_not_found(applying_client: AsyncClient) -> None:
    response = await applying_client.post(f"{BASE}/adopt")

    assert response.status_code == 404


async def test_an_incomplete_discovery_is_refused(
    applying_client: AsyncClient, trino_cluster: DockerContainer
) -> None:
    """Nothing half-understood reaches the Cluster. The outstanding thing is made the way a
    real one arises: a catalog created by DDL, whose properties Apchi will not invent."""
    cluster = _cluster(trino_cluster)
    await cluster.create_catalog("unaccounted", "tpch", {})
    try:
        await applying_client.post(f"{BASE}/discoveries")

        response = await applying_client.post(f"{BASE}/adopt")

        assert response.status_code == 409
        assert "could not account for" in response.json()["message"]
    finally:
        await cluster.drop_catalog("unaccounted")


async def test_adopting_before_the_seed_is_refused_naming_the_secrets(
    applying_client: AsyncClient, trino_cluster: DockerContainer
) -> None:
    """A cutover done without a seed points the coordinator at Secrets that do not say what
    was discovered. Catching it here means saying "seed first" rather than failing an Apply
    halfway and rolling a Cluster back."""
    cluster = _cluster(trino_cluster)
    await cluster.create_catalog("legacy", "tpch", {})
    try:
        await applying_client.post(f"{BASE}/discoveries")
        await applying_client.put(
            f"{BASE}/discovery/catalogs/legacy",
            json={"name": "legacy", "connector": "tpch", "properties": {}},
        )

        response = await applying_client.post(f"{BASE}/adopt")

        assert response.status_code == 409
        assert "seed first" in response.json()["message"]
    finally:
        await cluster.drop_catalog("legacy")


async def test_adopting_before_the_cutover_is_refused(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The refusal your correction created.

    Before the cutover the coordinator is still reading the Admin's files, so there is
    nothing for Verification to verify -- and a Snapshot claiming otherwise would be exactly
    the never-verified Snapshot §15 refuses to create.
    """
    fake_kubernetes.secrets["their-rules"] = {"rules.json": '{"catalogs": [{"allow": "all"}]}'}
    fake_kubernetes.config_maps["trino-config"]["access-control.properties"] = (
        "access-control.name=file\n"
        "security.config-file=/etc/trino/theirs/rules.json\n"
        "security.refresh-period=1s\n"
    )
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"].append(
        {"name": "their-rules", "mountPath": "/etc/trino/theirs"}
    )
    fake_kubernetes.pod_spec["volumes"].append(
        {"name": "their-rules", "secret": {"secretName": "their-rules"}}
    )
    await applying_client.post(f"{BASE}/discoveries")
    await applying_client.post(f"{BASE}/seed")

    response = await applying_client.post(f"{BASE}/adopt")

    assert response.status_code == 409
    message = response.json()["message"]
    assert "cutover has not happened" in message
    assert "nothing for Verification to verify" in message


async def test_a_cluster_that_already_has_snapshots_is_refused(
    applying_client: AsyncClient,
) -> None:
    """Adoption produces Snapshot 1 and runs once. A re-sync producing Snapshot N+1 is not
    adoption -- it is editing the Candidate, which is already possible, and doing it this way
    would silently replace whatever an Operator had staged."""
    await _ready_to_adopt(applying_client)
    first = await applying_client.post(f"{BASE}/adopt")
    await _settled(applying_client, first.json()["id"])

    response = await applying_client.post(f"{BASE}/adopt")

    assert response.status_code == 409
    assert "already managed" in response.json()["message"]


# --- what it produces ---------------------------------------------------------------


async def test_adopting_produces_snapshot_one_through_the_ordinary_lifecycle(
    applying_client: AsyncClient,
) -> None:
    """Validated, applied and verified like every other Snapshot. The stage history is the
    evidence: it is the same pipeline, not a special path."""
    await _ready_to_adopt(applying_client)

    started = await applying_client.post(f"{BASE}/adopt")
    assert started.status_code == 202
    record = await _settled(applying_client, started.json()["id"])

    assert record["stage"] == "succeeded", record["failure_reason"]
    assert record["snapshot"] == 1
    assert [entry["stage"] for entry in record["history"]][:2] == ["validating", "applying"]
    assert "verifying" in [entry["stage"] for entry in record["history"]]


async def test_the_snapshot_holds_what_was_discovered(applying_client: AsyncClient) -> None:
    await _ready_to_adopt(applying_client)
    discovery = (await applying_client.get(f"{BASE}/discovery")).json()
    started = await applying_client.post(f"{BASE}/adopt")
    await _settled(applying_client, started.json()["id"])

    snapshot = (await applying_client.get("/api/v1/snapshots/1")).json()

    for section in discovery["sections"]:
        assert snapshot["sections"][section["section"]] == section["resources"]


async def test_the_admin_values_discovered_are_written_and_are_not_in_the_snapshot(
    applying_client: AsyncClient,
) -> None:
    """Invariant 9: applied, never recorded in a Snapshot, surviving a rollback. The
    preserved access-control rules are why a Cluster keeps the access it had on the day it
    was onboarded, and a rollback must not take it away."""
    await _ready_to_adopt(applying_client)
    started = await applying_client.post(f"{BASE}/adopt")
    await _settled(applying_client, started.json()["id"])

    enforcement = (await applying_client.get("/api/v1/admin/permissions/enforcement")).json()
    snapshot = (await applying_client.get("/api/v1/snapshots/1")).json()

    assert "enforced" in enforcement
    assert "preserved_access_control" not in snapshot["sections"]
    assert "enforce_permissions" not in snapshot["sections"]


async def test_a_failed_adoption_leaves_no_snapshot_and_can_be_retried(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Nothing special is needed for this -- no Snapshot exists until the Apply verifies --
    but "nothing special is needed" is a claim, so it gets a test."""
    await _ready_to_adopt(applying_client)
    fake_kubernetes.report_a_missing_worker_once = True

    failed = await _settled(
        applying_client, (await applying_client.post(f"{BASE}/adopt")).json()["id"]
    )

    assert failed["stage"] == "failed"
    assert failed["snapshot"] is None
    assert (await applying_client.get("/api/v1/snapshots")).json()["items"] == []

    retried = await _settled(
        applying_client, (await applying_client.post(f"{BASE}/adopt")).json()["id"]
    )

    assert retried["stage"] == "succeeded", retried["failure_reason"]
    assert retried["snapshot"] == 1


async def test_a_catalog_already_live_and_seeded_is_not_a_collision_with_itself(
    applying_client: AsyncClient, trino_cluster: DockerContainer
) -> None:
    """The adopting Apply's catalogs are, by definition, catalogs that already exist.

    Found by tests/tier2/test_adoption.py against a hand-configured Cluster: adoption got
    all the way to its Apply and then failed Validation with "A catalog named 'bench'
    already exists on the Cluster and is not managed by Apchi. Adopt it or choose another
    name." -- raised by the check whose entire purpose is to stop an Operator creating a
    catalog over one nobody manages, firing on the operation that manages them.

    The plan measured the Candidate against the Snapshot it derives from, and adoption has
    none, so every discovered catalog looked new. The seed had already written the Secret
    and the cutover had already loaded it, so every one of them was live.

    Tier 1's own fixture cannot catch this: its Trino starts with no catalogs at all, so
    there is nothing for a discovered catalog to collide with. Here one is made the way a
    real one is -- by DDL, going round Apchi -- and then supplied, which is exactly the
    state the seed leaves behind.
    """
    cluster = _cluster(trino_cluster)
    await cluster.create_catalog("legacy", "tpch", {})
    try:
        await applying_client.post(f"{BASE}/discoveries")
        # Trino will not report a catalog's properties back (#88), so adoption asks. This is
        # the Operator answering, and it makes the discovery complete.
        amended = await applying_client.put(
            f"{BASE}/discovery/catalogs/legacy",
            json={"name": "legacy", "connector": "tpch", "properties": {}},
        )
        assert amended.status_code == 200, amended.json()
        assert amended.json()["complete"], amended.json()["outstanding"]
        seeded = await applying_client.post(f"{BASE}/seed")
        assert seeded.status_code == 200, seeded.json()

        started = await applying_client.post(f"{BASE}/adopt")
        assert started.status_code == 202, started.json()
        record = await _settled(applying_client, started.json()["id"])

        assert record["stage"] == "succeeded", record["failure_reason"]
        assert (await applying_client.get("/api/v1/snapshots/1")).json()["sections"]["catalogs"][
            "legacy"
        ]["connector"] == "tpch"
        assert "legacy" in await cluster.catalogs(), (
            "and the catalog is still there -- the narrowing must skip the CREATE, not the catalog"
        )
    finally:
        await cluster.drop_catalog("legacy")

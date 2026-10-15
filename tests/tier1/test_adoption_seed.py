"""The seed: Apchi writes its own Secrets, and nothing else happens.

Step 3 of five. The Cluster goes on serving exactly what it was serving, because Apchi writes
copies of its own and never the files it read. That is what makes the whole onboarding
reversible, and it is the one thing here worth more than a passing test -- so it is asserted
positively rather than assumed: seed, confirm the originals are untouched, delete Apchi's
Secrets, confirm nothing moved.

Two things the seed must not do, both easy to do by accident: patch the coordinator's pod
template, which would cut over at a moment nobody chose, and write a Snapshot, which would
claim a verification that cannot have happened yet.
"""

import copy

from httpx import AsyncClient
from testcontainers.core.container import DockerContainer

from app.adapters.trino import Trino
from tests.conftest import FakeKubernetes

BASE = "/api/v1/admin/adoption"


def _cluster(container: DockerContainer) -> Trino:
    """A client for the container standing in for the Cluster, to create a catalog the way a
    real one appears: by DDL, going round Apchi."""
    return Trino(
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(8080)),
    )


async def _complete_discovery(applying_client: AsyncClient) -> dict:
    """A discovery with nothing outstanding, which is what the seed requires.

    Needs a reachable Trino, so these tests use `applying_client`: without one, catalogs
    cannot be reconciled and the discovery is permanently incomplete -- which is correct, and
    is #88's point rather than something to work around here.
    """
    created = await applying_client.post(f"{BASE}/discoveries")
    assert created.status_code == 201, created.json()
    view = created.json()
    assert view["complete"], f"not complete: {view['outstanding']}"
    return view


# --- what it refuses ----------------------------------------------------------------


async def test_seeding_without_a_discovery_is_not_found(applying_client: AsyncClient) -> None:
    response = await applying_client.post(f"{BASE}/seed")

    assert response.status_code == 404


async def test_a_discovery_with_anything_outstanding_is_refused(
    applying_client: AsyncClient, trino_cluster: DockerContainer
) -> None:
    """Same reason #91 refuses to adopt one: a Secret written from a half-read Cluster is a
    Cluster Apchi would misconfigure at the cutover.

    The outstanding thing is made the way a real one arises -- a catalog created by DDL,
    whose properties live only in the coordinator's store and which Apchi will not invent.
    """
    cluster = _cluster(trino_cluster)
    await cluster.create_catalog("unreadable_by_apchi", "tpch", {})
    try:
        await applying_client.post(f"{BASE}/discoveries")

        response = await applying_client.post(f"{BASE}/seed")

        assert response.status_code == 409
        assert "could not account for" in response.json()["message"]
    finally:
        await cluster.drop_catalog("unreadable_by_apchi")


# --- what it writes, and what it does not -------------------------------------------


async def test_the_seed_writes_apchis_secrets(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings
) -> None:
    await _complete_discovery(applying_client)

    body = (await applying_client.post(f"{BASE}/seed")).json()

    assert settings.catalog_secret_name in body["secrets"]
    assert settings.access_control_secret_name in body["secrets"]
    assert settings.certificate_mapping_secret_name in body["secrets"]
    assert settings.access_control_secret_name in fake_kubernetes.secrets


async def test_the_seed_does_not_patch_the_coordinators_pod_template(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Four Sections declare a CoordinatorFile, so a normal Apply mounts files and triggers a
    Rollout. A seed that did that would cut over at a moment nobody chose, on a Cluster still
    serving the Admin's configuration."""
    await _complete_discovery(applying_client)
    before = copy.deepcopy(fake_kubernetes.pod_spec)

    await applying_client.post(f"{BASE}/seed")

    assert fake_kubernetes.pod_spec == before
    assert fake_kubernetes.restarts == []


async def test_the_seed_writes_no_snapshot_and_promotes_no_candidate(
    applying_client: AsyncClient,
) -> None:
    """A Snapshot is a configuration applied to the real Cluster and verified. After a seed
    nothing has been applied: the Cluster is still reading the Admin's files."""
    await _complete_discovery(applying_client)

    await applying_client.post(f"{BASE}/seed")

    assert (await applying_client.get("/api/v1/snapshots")).json()["items"] == []
    assert (await applying_client.get("/api/v1/applies")).json()["items"] == []
    assert (await applying_client.get("/api/v1/review")).json()["has_changes"] is False


async def test_the_files_apchi_read_from_are_not_written(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The claim the whole design rests on. Apchi's copies are its own, so the Cluster is
    still serving what it was serving and the Admin's originals are there to go back to."""
    fake_kubernetes.config_maps["their-config"] = {"rules.json": '{"catalogs": [{"allow": "all"}]}'}
    fake_kubernetes.secrets["their-secret"] = {"user-mapping.json": '{"rules": []}'}
    before_config_maps = copy.deepcopy(fake_kubernetes.config_maps)
    before_theirs = copy.deepcopy(fake_kubernetes.secrets["their-secret"])
    await _complete_discovery(applying_client)

    await applying_client.post(f"{BASE}/seed")

    assert fake_kubernetes.config_maps == before_config_maps
    assert fake_kubernetes.secrets["their-secret"] == before_theirs


async def test_seeding_is_reversible_by_deleting_what_it_wrote(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings
) -> None:
    """The reversibility claim, asserted rather than assumed. An Admin has to be able to
    change their mind before the cutover, and nothing the seed did should outlive that."""
    fake_kubernetes.config_maps["their-config"] = {"rules.json": "{}"}
    before = (
        copy.deepcopy(fake_kubernetes.config_maps),
        copy.deepcopy(fake_kubernetes.pod_spec),
    )
    await _complete_discovery(applying_client)

    written = (await applying_client.post(f"{BASE}/seed")).json()["secrets"]
    for name in written:
        fake_kubernetes.secrets.pop(name, None)

    assert (
        copy.deepcopy(fake_kubernetes.config_maps),
        copy.deepcopy(fake_kubernetes.pod_spec),
    ) == before


# --- seeding twice ------------------------------------------------------------------


async def test_seeding_twice_changes_nothing_and_says_so(
    applying_client: AsyncClient, trino_cluster: DockerContainer
) -> None:
    """An Admin unsure whether they seeded should be able to find out by doing it again.

    A catalog is adopted first so the seed has something to write: with nothing discovered
    the first seed is already a no-op, which would make this test pass for the wrong reason.
    """
    cluster = _cluster(trino_cluster)
    await cluster.create_catalog("legacy", "tpch", {})
    try:
        await applying_client.post(f"{BASE}/discoveries")
        await applying_client.put(
            f"{BASE}/discovery/catalogs/legacy",
            json={"name": "legacy", "connector": "tpch", "properties": {}},
        )

        first = (await applying_client.post(f"{BASE}/seed")).json()
        second = (await applying_client.post(f"{BASE}/seed")).json()

        assert first["changed"], "the first seed should have written the discovered catalog"
        assert second["changed"] == []
        assert second["secrets"] == first["secrets"]
    finally:
        await cluster.drop_catalog("legacy")


# --- what the cutover still needs ---------------------------------------------------


async def test_a_cluster_already_cut_over_needs_no_cutover_change(
    applying_client: AsyncClient,
) -> None:
    """The healthy fixture mirrors deploy/trino-dev, which is what a Cluster looks like
    *after* the Admin's deployment change -- so there is nothing left to do.

    The first version of this check demanded every file Apchi declares be mounted, which made
    a correctly cut-over Cluster look as though nothing had been done: Apchi mounts those
    itself, and for Event Listeners and Resource Groups the absence of the mount is how "none
    configured" is expressed.
    """
    await _complete_discovery(applying_client)

    assert (await applying_client.post(f"{BASE}/seed")).json()["cutover"] == []


async def test_the_seed_says_what_the_cutover_must_change(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Apchi does not perform the cutover -- it is the Admin's deployment. What it owes them
    is knowing exactly what to change.

    Set up the way a Cluster being onboarded is: Trino reads its rules from a file of the
    Admin's, somewhere Apchi does not write.
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
    await _complete_discovery(applying_client)

    cutover = (await applying_client.post(f"{BASE}/seed")).json()["cutover"]

    assert [change["path"] for change in cutover] == ["/etc/trino/theirs/rules.json"]
    assert "Apchi writes" in cutover[0]["why"]


async def test_the_catalog_seed_initcontainer_is_named_as_a_change_that_comes_after(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings
) -> None:
    """The one change that must not be made first. Adding it before the seed means the next
    restart copies an empty Secret over the catalog store and every catalog disappears."""
    fake_kubernetes.pod_spec["initContainers"] = []
    await _complete_discovery(applying_client)

    cutover = (await applying_client.post(f"{BASE}/seed")).json()["cutover"]
    seeding = [c for c in cutover if c["secret"] == settings.catalog_secret_name]

    assert seeding
    assert "must come after the seed" in seeding[0]["why"]
    assert "loses every catalog" in seeding[0]["why"]


async def test_a_subpath_mount_of_an_apchi_managed_file_is_named_in_the_cutover(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """A subPath mount is frozen at pod creation, so Apchi's later writes never arrive --
    which is the §16 precondition expressed as the change to make."""
    mounts = fake_kubernetes.pod_spec["containers"][0]["volumeMounts"]
    for mount in mounts:
        if mount["mountPath"] == "/etc/trino/access-control":
            mount["subPath"] = "rules.json"
    await applying_client.post(f"{BASE}/discoveries")
    view = (await applying_client.get(f"{BASE}/discovery")).json()
    for problem in view["outstanding"]:
        if problem["section"] and problem["path"]:
            await applying_client.delete(f"{BASE}/discovery/{problem['section']}/{problem['path']}")

    response = await applying_client.post(f"{BASE}/seed")

    # Either the discovery refuses it as a precondition or the cutover names it; both are
    # correct, and what must not happen is silence.
    if response.status_code == 200:
        assert any("subPath" in change["why"] for change in response.json()["cutover"])
    else:
        assert response.status_code == 409


async def test_the_seed_names_what_it_read_from_and_left_alone(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """So an Admin can see which files are still the ones in force.

    Set up the way an un-adopted Cluster is: the mapping file in a Secret of the Admin's, at
    a path of their choosing. Apchi reads it and writes its own copy elsewhere, and the
    Admin's Secret has to come back named as untouched -- otherwise nothing tells them the
    Cluster is still serving the original.
    """
    fake_kubernetes.secrets["their-mapping"] = {"mapping.json": '{"rules": []}'}
    fake_kubernetes.config_maps["trino-config"]["coordinator-config.properties"] = (
        "http-server.authentication.insecure.user-mapping.file=/etc/trino/theirs/mapping.json\n"
    )
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"].append(
        {"name": "their-mapping", "mountPath": "/etc/trino/theirs"}
    )
    fake_kubernetes.pod_spec["volumes"].append(
        {"name": "their-mapping", "secret": {"secretName": "their-mapping"}}
    )
    await _complete_discovery(applying_client)

    body = (await applying_client.post(f"{BASE}/seed")).json()

    assert "their-mapping" in body["untouched"]
    assert fake_kubernetes.secrets["their-mapping"] == {"mapping.json": '{"rules": []}'}


async def test_the_cutover_is_judged_by_the_live_coordinator_not_the_stored_reading(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The cutover check has to clear once the Admin has cut over, and the discovery it is
    judging still records the Admin's old paths.

    Found by tests/tier2/test_adoption.py: after a real cutover, `POST /adopt` went on
    refusing with "the cutover has not happened: /etc/trino/theirs/user-mapping.json". The
    check was reading `looked_at` out of the stored discovery, which is by definition where
    Trino read *before* -- and that discovery is deliberately never re-read (#82), because
    re-reading mid-migration finds a Cluster whose resource groups have been taken away and
    whose replacements Apchi does not mount until the Apply. So the one question worth asking
    is where the coordinator reads from *now*.
    """
    fake_kubernetes.secrets["their-rules"] = {"rules.json": '{"catalogs": [{"allow": "all"}]}'}
    theirs = (
        "access-control.name=file\n"
        "security.config-file=/etc/trino/theirs/rules.json\n"
        "security.refresh-period=1s\n"
    )
    fake_kubernetes.config_maps["trino-config"]["access-control.properties"] = theirs
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"].append(
        {"name": "their-rules", "mountPath": "/etc/trino/theirs"}
    )
    fake_kubernetes.pod_spec["volumes"].append(
        {"name": "their-rules", "secret": {"secretName": "their-rules"}}
    )
    await _complete_discovery(applying_client)

    assert [
        change["path"] for change in (await applying_client.post(f"{BASE}/seed")).json()["cutover"]
    ] == ["/etc/trino/theirs/rules.json"]

    # The Admin's deployment change: Trino now reads Apchi's file. Nothing re-reads the
    # discovery, which still says the rules were found under /etc/trino/theirs.
    fake_kubernetes.config_maps["trino-config"]["access-control.properties"] = (
        "access-control.name=file\n"
        "security.config-file=/etc/trino/access-control/rules.json\n"
        "security.refresh-period=1s\n"
    )

    seeded = (await applying_client.post(f"{BASE}/seed")).json()
    assert seeded["cutover"] == [], (
        "the coordinator reads Apchi's rules now, so there is nothing left for the Admin "
        "to change -- however the stored reading found them"
    )

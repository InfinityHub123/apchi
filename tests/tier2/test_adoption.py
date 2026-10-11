"""Adopting a Trino nobody configured with Apchi in mind.

Every other tier 2 test runs against `deploy/trino-dev`, whose access-control and
user-mapping files Apchi generates. A discovery that round-trips a file Apchi wrote proves
the generator is deterministic; it proves nothing about reading somebody else's Trino, which
is the whole question §15 asks. So this runs against `deploy/trino-adopted` -- hand-written,
in paths Apchi would not have chosen, with rules Apchi's models cannot hold.

The assertion that matters is not that discovery produced the right document. It is that
after adopting, **the Cluster behaves the way it did before**: the same catalogs load and
answer queries, queries land in the resource group the Admin's selectors sent them to, and a
coordinator restart loses nothing.

That last one is the failure §82 exists to prevent -- a catalog Apchi does not hold
disappearing weeks later at an unrelated restart -- and the only test that actually proves it
does not happen.

Note what this does *not* do after the cutover: re-read the Cluster. The discovery adopted is
the one completed before, and the two defects that taught that are both recorded below -- the
cutover takes the Admin's resource groups away before Apchi mounts its own, so a fresh reading
finds none, and the cutover check has to ask the live coordinator where it reads from rather
than the stored reading.
"""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.config import Settings
from tests.tier2.conftest import (
    ForwardedKubernetes,
    PortForward,
    RealKubernetes,
    kubectl,
    running_apchi,
)

ADOPTED = Path(__file__).resolve().parents[2] / "deploy" / "trino-adopted"
BASE = "/api/v1/admin/adoption"
TERMINAL = {"succeeded", "failed", "incident"}

#: Apchi's Secrets for this Cluster, named so nothing collides with deploy/trino-dev, whose
#: baseline fixture shares the namespace and would otherwise report them as drift.
APCHI_SECRETS = (
    "adopted-catalog-seed",
    "adopted-access-control",
    "adopted-user-mapping",
    "adopted-client-certificates",
    "adopted-event-listener",
    "adopted-resource-groups",
)


def _settings(base: Settings) -> Settings:
    """Apchi configured for the hand-made Cluster rather than for deploy/trino-dev.

    Derived from the suite's own settings rather than built fresh, so it keeps the MongoDB
    the tests run against -- a fresh Settings takes the production default and spends thirty
    seconds failing to reach localhost.
    """
    return base.model_copy(
        update=dict(
            trino_host="adopted",
            coordinator_deployment_name="adopted-coordinator",
            worker_deployment_name="adopted-worker",
            catalog_secret_name="adopted-catalog-seed",
            access_control_secret_name="adopted-access-control",
            certificate_mapping_secret_name="adopted-user-mapping",
            client_certificate_secret_name="adopted-client-certificates",
            event_listener_secret_name="adopted-event-listener",
            resource_groups_secret_name="adopted-resource-groups",
        )
    )


def _query(sql: str, user: str = "apchi") -> str:
    return kubectl(
        "exec",
        "deploy/adopted-coordinator",
        "-c",
        "trino",
        "--",
        "trino",
        "--user",
        user,
        "--execute",
        sql,
    )


@pytest.fixture
def hand_configured(cluster: None) -> Iterator[None]:
    """The Cluster as somebody else built it, torn down afterwards.

    Everything it creates is prefixed `adopted-`, including the Secrets Apchi seeds, so a run
    that dies part-way cannot leave the baseline fixture reporting drift on deploy/trino-dev.
    """
    # The directory, which is the Cluster as it was before Apchi. The cutover manifest is in
    # a subdirectory so this cannot pick it up: applied here it points the coordinator at
    # Secrets that do not exist yet and Trino refuses to boot.
    kubectl("apply", "-f", str(ADOPTED))
    kubectl("rollout", "status", "deploy/adopted-coordinator", "--timeout=600s")
    kubectl("rollout", "status", "deploy/adopted-worker", "--timeout=600s")
    try:
        yield
    finally:
        kubectl("delete", "-f", str(ADOPTED), "--ignore-not-found")
        for secret in APCHI_SECRETS:
            kubectl("delete", "secret", secret, "--ignore-not-found")


async def _settled(client: AsyncClient, apply_id: str) -> dict:
    deadline = asyncio.get_running_loop().time() + 600
    while asyncio.get_running_loop().time() < deadline:
        record = (await client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            return record
        await asyncio.sleep(0.2)
    raise AssertionError("the adoption never settled")


def _cut_over() -> None:
    """The Admin's deployment change: step 4 of five.

    A committed manifest rather than patches, so what the cutover *is* can be read as a diff
    against the files it replaces.

    The diagnostics exist because this is the step most likely to fail on a loaded machine --
    it replaces the coordinator -- and the fixture tears the Cluster down on failure, so
    without them there is nothing left to look at.
    """
    kubectl("apply", "-f", str(ADOPTED / "cutover" / "trino.yaml"))
    try:
        kubectl("rollout", "status", "deploy/adopted-coordinator", "--timeout=300s")
    except Exception:
        print("\n--- the cutover rollout did not finish ---")
        print(kubectl("get", "pods", "-l", "app=adopted", "-o", "wide"))
        print(kubectl("describe", "deploy/adopted-coordinator"))
        for line in kubectl(
            "get", "events", "--sort-by=.lastTimestamp", "--no-headers"
        ).splitlines()[-25:]:
            print(line)
        raise


async def test_a_cluster_apchi_did_not_configure_can_be_adopted(
    hand_configured: None, real_kubernetes: RealKubernetes, settings: Settings
) -> None:
    """The whole of §15, end to end, against a Trino configured by hand."""
    settings = _settings(settings)

    # No DDL-created catalog here, and the reason is worth stating: this Cluster keeps its
    # catalogs as a read-only ConfigMap, so CREATE CATALOG fails outright with "Could not
    # store catalog properties". A Cluster cannot have both -- DDL needs a writable store,
    # and a writable store is one Apchi cannot read catalog properties out of.
    #
    # So the two halves of #88 are proven on the two Clusters that can each have one. Here,
    # where the catalogs are files, Apchi recovers their properties in full. The DDL-only
    # path -- a catalog Apchi can name and cannot reconstruct -- is proven against a real
    # Trino in tests/tier1/test_adoption_discovery.py, where the store is writable.

    forward = PortForward("svc/adopted", 8080)
    forward.start()
    try:
        kubernetes = ForwardedKubernetes(real_kubernetes, cluster=forward)
        async with running_apchi(settings, kubernetes, forward) as apchi:
            # --- 2. discover ------------------------------------------------------
            view = (await apchi.post(f"{BASE}/discoveries")).json()

            catalogs = next(s for s in view["sections"] if s["section"] == "catalogs")
            assert set(catalogs["resources"]) == {"bench", "notes"}, (
                "the catalogs mounted as files should be read with their properties"
            )
            assert catalogs["resources"]["bench"]["properties"] == {"tpch.splits-per-node": "2"}, (
                "properties must come back exactly, not approximately"
            )

            # The rules Apchi's models cannot hold, kept rather than dropped (#89).
            preserved = view["admin_values"]["preserved_access_control"]
            assert preserved["impersonation"] == [{"original_user": "^svc_.*$", "new_user": ".*"}]
            assert any(
                rule.get("catalog") == "^(bench|notes)$" for rule in preserved.get("tables", [])
            ), "a rule matching a set of catalogs cannot be a grant, so it must be preserved"

            # The second mapping pattern, which Apchi has no room for (§13.3).
            mapping = next(s for s in view["sections"] if s["section"] == "certificate_mapping")
            assert mapping["resources"], "the first pattern should become the Operator's"

            # --- 3. nothing to supply, because every catalog was readable ---------
            assert view["complete"], view["outstanding"]

            # --- 4. seed ----------------------------------------------------------
            before_theirs = json.loads(kubectl("get", "configmap", "adopted-theirs", "-o", "json"))[
                "data"
            ]
            seeded = (await apchi.post(f"{BASE}/seed")).json()
            assert seeded["changed"], "the seed should have written Apchi's Secrets"
            assert seeded["cutover"], "the deployment still reads the Admin's files"

            # Inert: the Cluster is still serving exactly what it was.
            assert (
                json.loads(kubectl("get", "configmap", "adopted-theirs", "-o", "json"))["data"]
                == before_theirs
            ), "the Admin's own files must not be touched"
            assert {
                name.strip('"') for name in _query("SHOW CATALOGS").split() if name.strip('"')
            } >= {"bench", "notes"}

            # Adopting now would be adopting a Cluster that is not reading Apchi's files.
            refused = await apchi.post(f"{BASE}/adopt")
            assert refused.status_code == 409
            assert "cutover has not happened" in refused.json()["message"]

        # --- 5. cut over, then adopt ----------------------------------------------
        _cut_over()
        forward.stop()
        forward = PortForward("svc/adopted", 8080)
        forward.start()
        kubernetes = ForwardedKubernetes(real_kubernetes, cluster=forward)

        async with running_apchi(settings, kubernetes, forward) as apchi:
            # Deliberately NOT re-read. The discovery to adopt is the one completed before
            # the cutover; re-reading here reads a Cluster mid-migration, because the cutover
            # took the Admin's resource-groups.properties away and Apchi does not mount its
            # own until an Apply. For that moment Trino has no resource groups at all, so a
            # fresh reading finds none -- and adopting that would drop them.
            #
            # Found by writing this test with a re-read in it: it failed with "Apchi's
            # Secrets do not hold this discovery yet (adopted-resource-groups)", because the
            # seed held the groups and the new reading did not.
            existing = (await apchi.get(f"{BASE}/discovery")).json()
            assert existing["complete"], existing["outstanding"]
            groups = next(s for s in existing["sections"] if s["section"] == "resource_groups")
            assert set(groups["resources"]) >= {"reports"}, (
                "the discovery being adopted is the pre-cutover one, which still holds the "
                "Admin's resource groups"
            )

            started = await apchi.post(f"{BASE}/adopt")
            assert started.status_code == 202, started.json()
            record = await _settled(apchi, started.json()["id"])
            assert record["stage"] == "succeeded", record["failure_reason"]
            assert record["snapshot"] == 1

            snapshot = (await apchi.get("/api/v1/snapshots/1")).json()
            assert set(snapshot["sections"]["catalogs"]) == {"bench", "notes"}

        # --- what an End User can observe -----------------------------------------
        serving = {name.strip('"') for name in _query("SHOW CATALOGS").split() if name.strip('"')}
        assert {"bench", "notes"} <= serving

        # The store is writable now, so DDL works where it could not before -- and a catalog
        # made this way is one Apchi does not hold, which is the §10 divergence the next
        # restart resolves by losing it. Created here so the restart assertion below is about
        # Apchi's seed rather than about the store happening to survive.
        _query("CREATE CATALOG scratch USING memory", user="platform")
        assert "scratch" in _query("SHOW CATALOGS")

        assert "1" in _query("SELECT 1 FROM bench.tiny.nation LIMIT 1")

        # The Admin's selectors still route queries where they did.
        assert "reports" in _query(
            "SELECT resource_group_id FROM system.runtime.queries ORDER BY created DESC LIMIT 1"
        )

        # --- the failure this whole slice exists to prevent ------------------------
        kubectl("rollout", "restart", "deploy/adopted-coordinator")
        kubectl("rollout", "status", "deploy/adopted-coordinator", "--timeout=600s")

        after_restart = {
            name.strip('"') for name in _query("SHOW CATALOGS").split() if name.strip('"')
        }
        assert {"bench", "notes"} <= after_restart, (
            "the catalogs Apchi adopted must survive the restart that reseeds the store. This "
            "is the silent, weeks-later failure §10 describes -- before adoption these lived "
            "in a ConfigMap and would simply have gone -- and it is the only assertion here "
            "that proves adoption did its job."
        )
        assert "scratch" not in after_restart, (
            "and the catalog Apchi does not hold is gone, which is the same mechanism from "
            "the other side: the store is reseeded from Apchi's Secret and holds nothing else."
        )
    finally:
        forward.stop()

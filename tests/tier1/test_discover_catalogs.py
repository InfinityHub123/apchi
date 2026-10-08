"""Catalog discovery: three sources, none of them complete.

The asymmetric Section. Trino will say a catalog's name and its connector and nothing else --
no `SHOW CREATE CATALOG` in 483, no properties column, verified against a running coordinator
-- while the coordinator's writable store holds the properties and Apchi cannot read it. So a
catalog can be known to exist and be impossible to reconstruct, and the only safe answer is to
say so rather than to invent a connection.
"""

from pathlib import Path

from app.config import Settings
from app.pipeline.discovery import Discovery, discover
from tests.conftest import FakeKubernetes

ROOT = Path(__file__).resolve().parents[2]
STORE = "/etc/trino/catalog"


class Live:
    """What Trino reports from system.metadata.catalogs."""

    def __init__(self, catalogs: dict[str, str]) -> None:
        self.catalogs = catalogs

    async def catalog_connectors(self) -> dict[str, str]:
        return self.catalogs


class Unreachable:
    async def catalog_connectors(self) -> dict[str, str]:
        raise RuntimeError("Connection refused")


def _catalogs(discovery: Discovery):
    return next(found for found in discovery.sections if found.section == "catalogs")


def _mount_store(fake: FakeKubernetes, files: dict[str, str]) -> None:
    """A Cluster keeping its catalogs the way an un-adopted one usually does: a ConfigMap
    mounted over the store directory, which Apchi can read."""
    fake.config_maps["their-catalogs"] = files
    fake.config_maps["trino-config"]["catalog-store.properties"] = f"catalog.config-dir={STORE}\n"
    fake.pod_spec["containers"][0]["volumeMounts"] += [
        {
            "name": "config",
            "mountPath": "/etc/trino/catalog-store.properties",
            "subPath": "catalog-store.properties",
        },
        {"name": "their-catalogs", "mountPath": STORE},
    ]
    fake.pod_spec["volumes"].append(
        {"name": "their-catalogs", "configMap": {"name": "their-catalogs"}}
    )


PG = "connector.name=postgresql\nconnection-url=jdbc:postgresql://db:5432/f\nconnection-user=t\n"


# --- the three cases ----------------------------------------------------------------


async def test_a_catalog_in_a_readable_source_and_in_trino_is_complete(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    _mount_store(fake_kubernetes, {"finance.properties": PG})

    discovery = await discover(fake_kubernetes, settings, Live({"finance": "postgresql"}))
    found = _catalogs(discovery)

    assert found.resources["finance"] == {
        "connector": "postgresql",
        "properties": {
            "connection-url": "jdbc:postgresql://db:5432/f",
            "connection-user": "t",
        },
    }
    assert found.problems == []


async def test_a_catalog_only_trino_knows_about_is_reported_incomplete(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Created by DDL after the pod started, so its properties exist only in the
    coordinator's writable store. Trino is the only witness that it exists at all."""
    _mount_store(fake_kubernetes, {})

    discovery = await discover(fake_kubernetes, settings, Live({"scratch": "memory"}))
    found = _catalogs(discovery)

    assert found.resources == {}
    assert [(p.kind, p.path) for p in found.problems] == [("incomplete", "scratch")]
    assert "memory" in found.problems[0].detail
    assert "will not invent" in found.problems[0].detail


async def test_no_property_is_invented_for_an_incomplete_catalog(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The feature, not the limitation. An adopted catalog with a plausible wrong
    connection-url validates, applies, and fails at query time against the wrong database."""
    _mount_store(fake_kubernetes, {})

    discovery = await discover(fake_kubernetes, settings, Live({"warehouse": "postgresql"}))

    assert _catalogs(discovery).resources == {}


async def test_a_catalog_trino_is_not_serving_is_reported_as_not_loaded(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Configured and not live, which usually means no restart since it was added."""
    _mount_store(fake_kubernetes, {"finance.properties": PG})

    discovery = await discover(fake_kubernetes, settings, Live({}))
    found = _catalogs(discovery)

    assert "finance" in found.resources
    assert [(p.kind, p.path) for p in found.problems] == [("not_loaded", "finance")]
    assert "has not restarted" in found.problems[0].detail


# --- what must never happen ---------------------------------------------------------


async def test_system_is_never_adopted(settings: Settings, fake_kubernetes: FakeKubernetes) -> None:
    """It cannot be dropped and has no properties, so a Snapshot holding it would describe
    a Cluster Apchi could not restore -- which is why §10 already excludes it from
    rollback."""
    _mount_store(fake_kubernetes, {})

    discovery = await discover(fake_kubernetes, settings, Live({"system": "system"}))
    found = _catalogs(discovery)

    assert found.resources == {}
    assert found.problems == []


async def test_trino_being_unreachable_is_reported_not_ignored(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Without Trino a DDL-created catalog cannot be noticed at all, and would be gone at
    the first restart after adoption. Silence here would be the worst kind."""
    _mount_store(fake_kubernetes, {"finance.properties": PG})

    discovery = await discover(fake_kubernetes, settings, Unreachable())
    found = _catalogs(discovery)

    assert "finance" in found.resources
    assert [p.kind for p in found.problems] == ["unreadable"]
    assert "Connection refused" in found.problems[0].detail


async def test_discovery_without_trino_at_all_says_what_it_could_not_check(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    _mount_store(fake_kubernetes, {"finance.properties": PG})

    discovery = await discover(fake_kubernetes, settings)
    found = _catalogs(discovery)

    assert "finance" in found.resources
    assert [p.kind for p in found.problems] == ["unreadable"]
    assert "could not ask Trino" in found.problems[0].detail


# --- the seed, and the shapes a Cluster comes in ------------------------------------


async def test_the_seed_secret_is_read_as_well_as_the_store(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """On a Cluster part-way through onboarding the seed holds what Apchi put there, and the
    store holds what the Admin did. Both are read; the store wins, because it is what the
    coordinator is actually serving."""
    fake_kubernetes.secrets[settings.catalog_secret_name] = {
        "archive.properties": "connector.name=tpch\n"
    }
    _mount_store(fake_kubernetes, {"finance.properties": PG})

    discovery = await discover(
        fake_kubernetes, settings, Live({"finance": "postgresql", "archive": "tpch"})
    )
    found = _catalogs(discovery)

    assert sorted(found.resources) == ["archive", "finance"]
    assert f"Secret {settings.catalog_secret_name}" in found.looked_at


async def test_an_uncurated_connector_is_discovered_with_its_properties_passed_through(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Exactly as staging such a Catalog already does: Apchi curates seven connectors and
    anything else passes through, judged by Trino rather than by Apchi."""
    _mount_store(
        fake_kubernetes,
        {"events.properties": "connector.name=kinesis\nkinesis.access-key=abc\n"},
    )

    discovery = await discover(fake_kubernetes, settings, Live({"events": "kinesis"}))
    found = _catalogs(discovery)

    assert found.resources["events"] == {
        "connector": "kinesis",
        "properties": {"kinesis.access-key": "abc"},
    }


async def test_a_store_directory_apchi_cannot_read_is_not_an_error(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """On a Cluster Apchi already configured the store is an emptyDir, which has no content
    readable through the API. That is expected, and `system.metadata.catalogs` is what
    covers it -- so the catalogs still turn up, as incomplete."""
    discovery = await discover(fake_kubernetes, settings, Live({"sales": "tpch"}))
    found = _catalogs(discovery)

    assert found.resources == {}
    assert [(p.kind, p.path) for p in found.problems] == [("incomplete", "sales")]


async def test_a_properties_file_with_no_connector_is_refused_with_its_path(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    _mount_store(fake_kubernetes, {"broken.properties": "connection-url=jdbc:postgresql://db/f\n"})

    discovery = await discover(fake_kubernetes, settings, Live({}))
    found = _catalogs(discovery)

    assert found.resources == {}
    assert found.problems[0].kind == "unreadable"
    assert "connector.name" in found.problems[0].detail


async def test_a_file_in_the_store_apchi_does_not_own_is_reported(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    _mount_store(fake_kubernetes, {"finance.properties": PG, "README": "notes"})

    discovery = await discover(fake_kubernetes, settings, Live({"finance": "postgresql"}))
    found = _catalogs(discovery)

    assert "finance" in found.resources
    assert any("does not own" in p.detail for p in found.problems)


async def test_a_catalog_using_a_certificate_is_reported_because_the_name_is_lost(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The one loss the round-trip check cannot see, because it is semantic rather than
    textual. A certificate is wired into the connector's own properties and expanded into a
    path before anything is written, so the file holds a path and no record of the name --
    and re-rendering that path gives identical bytes.

    It matters because the refusal to remove a certificate a Catalog still references works
    off the name, so an unnamed one could be deleted out from under a working Catalog.
    """
    _mount_store(
        fake_kubernetes,
        {
            "finance.properties": (
                "connector.name=postgresql\n"
                "connection-url=jdbc:postgresql://db:5432/f\n"
                "sslcert=/etc/trino/certs/finance.crt\n"
                "sslkey=/etc/trino/certs/finance.key\n"
            )
        },
    )

    discovery = await discover(fake_kubernetes, settings, Live({"finance": "postgresql"}))
    found = _catalogs(discovery)

    # The Catalog still imports, and keeps working -- the path is right.
    assert found.resources["finance"]["properties"]["sslcert"] == "/etc/trino/certs/finance.crt"
    unaccounted = [p for p in found.problems if p.kind == "unaccounted"]
    assert [p.path for p in unaccounted] == ["finance"]
    assert "sslcert, sslkey" in unaccounted[0].detail


def test_discovery_needs_no_shell_access_to_the_coordinator() -> None:
    """No `pods/exec`. It would close the one hole -- the live store is readable with an exec
    and nothing else recovers a DDL-created catalog's properties -- but the privilege is not
    "read a file", it is "run any command in the coordinator", granted permanently to make
    one read easier during a one-time stage. #88 records the trade.
    """
    rbac = (ROOT / "charts" / "apchi" / "templates" / "rbac.yaml").read_text()

    assert "pods/exec" not in rbac
    assert "pods/log" in rbac

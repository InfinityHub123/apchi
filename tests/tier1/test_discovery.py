"""Discovery: reading a Cluster's configuration off the Cluster.

The first half of Adoption, and the half that must be safe to run against production. Two
properties matter more than anything it finds: it **writes nothing**, and it never reports a
Cluster as having less configuration than it has. The second is the dangerous one -- an
Operator who adopts a discovery that quietly missed something loses that something at the
next coordinator restart.
"""

import copy

import pytest

from app.config import Settings
from app.pipeline import discovery as discovery_module
from app.pipeline.discovery import Discovery, DiscoveryFailed, discover
from app.sections.certificate_mapping import RESOURCE as MAPPING
from app.sections.certificate_mapping.generator import MOUNT_PATH as MAPPING_PATH
from app.sections.certificate_mapping.generator import render_rules as render_mapping
from app.sections.client_certificates.generator import MOUNT_DIR, certificate_path, key_path
from app.sections.event_listeners.generator import MOUNT_PATH as LISTENER_PATH
from app.sections.resource_groups import SELECTORS
from app.sections.resource_groups.generator import (
    MANAGER_PATH,
)
from tests.certificates import certificate_pem, key_pair, key_pem
from tests.conftest import FakeKubernetes


def _of(discovery: Discovery, section: str):
    return next(found for found in discovery.sections if found.section == section)


def _kinds(discovery: Discovery) -> set[str]:
    return {problem.kind for problem in discovery.every_problem}


# --- the two properties that matter ------------------------------------------------


async def test_discovery_writes_nothing(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The whole point of discovery being separate from adoption. An Operator has to be
    able to run this against a Cluster that is serving, see what Apchi understood, and know
    that nothing happened."""
    before = (
        copy.deepcopy(fake_kubernetes.secrets),
        copy.deepcopy(fake_kubernetes.config_maps),
        copy.deepcopy(fake_kubernetes.pod_spec),
        copy.deepcopy(fake_kubernetes.pods),
    )

    await discover(fake_kubernetes, settings)

    assert (
        copy.deepcopy(fake_kubernetes.secrets),
        copy.deepcopy(fake_kubernetes.config_maps),
        copy.deepcopy(fake_kubernetes.pod_spec),
        copy.deepcopy(fake_kubernetes.pods),
    ) == before


async def test_a_cluster_apchi_cannot_read_fails_rather_than_discovering_nothing(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """An empty discovery of an unreachable Cluster looks exactly like a Cluster with
    nothing configured, and an Operator would adopt it and lose everything."""

    async def missing(deployment: str) -> dict:
        raise RuntimeError("deployments.apps 'trino-coordinator' not found")

    fake_kubernetes.deployment_pod_spec = missing  # type: ignore[method-assign]

    with pytest.raises(DiscoveryFailed, match="cannot read the coordinator Deployment"):
        await discover(fake_kubernetes, settings)


# --- what it reads ------------------------------------------------------------------


async def test_every_section_is_listed_even_with_nothing_configured(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    discovery = await discover(fake_kubernetes, settings)

    assert [found.section for found in discovery.sections] == [
        "catalogs",
        "client_certificates",
        "certificate_mapping",
        "event_listeners",
        "permissions",
        "resource_groups",
    ]


async def test_every_section_can_be_read_back_now(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Where Adoption got to: #86 did four, #88 catalogs, #89 permissions."""
    discovery = await discover(fake_kubernetes, settings)

    assert all(found.readable for found in discovery.sections)


async def test_a_section_apchi_could_not_parse_would_be_said_so_not_left_empty(
    settings: Settings, fake_kubernetes: FakeKubernetes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Section is unparseable today, so this uses a stub one to hold the guarantee that
    matters: a Section Apchi cannot read must be *reported*, because reporting it as empty
    would be reporting a Cluster with none of that configuration -- a lie with consequences,
    and the shape of every Adoption failure worth fearing."""

    class Unparseable:
        name = "resource_groups"

    monkeypatch.setattr(discovery_module, "REGISTERED", (Unparseable(),))

    discovery = await discover(fake_kubernetes, settings)

    found = _of(discovery, "resource_groups")
    assert found.readable is False
    assert found.resources == {}
    assert found.problems[0].kind == "unreadable_section"
    assert discovery.complete is False


async def test_the_certificate_mapping_is_found_where_trino_says_it_is(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Not where Apchi would put it: the authenticator's own property is the authority, and
    on an un-adopted Cluster it points wherever the Admin put the file."""
    fake_kubernetes.config_maps["trino-config"]["coordinator-config.properties"] = (
        "http-server.authentication.insecure.user-mapping.file=/etc/trino/theirs/mapping.json\n"
    )
    fake_kubernetes.secrets["their-mapping"] = {
        "mapping.json": render_mapping(
            {MAPPING: {"pattern": "CN=(.*?),.*", "user": "$1", "case": "keep"}},
            settings.trino_user,
        )
    }
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"].append(
        {"name": "their-mapping", "mountPath": "/etc/trino/theirs"}
    )
    fake_kubernetes.pod_spec["volumes"].append(
        {"name": "their-mapping", "secret": {"secretName": "their-mapping"}}
    )

    discovery = await discover(fake_kubernetes, settings)
    found = _of(discovery, "certificate_mapping")

    assert found.looked_at == ["/etc/trino/theirs/mapping.json"]
    assert found.resources[MAPPING]["pattern"] == "CN=(.*?),.*"
    assert found.guessed_because is None


async def test_apchi_says_when_it_guessed_a_path_rather_than_read_it(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """A Cluster whose authenticator names no mapping file is one Apchi looked for in the
    place it would put one. Worth saying, so an empty result is not mistaken for a fact."""
    discovery = await discover(fake_kubernetes, settings)
    found = _of(discovery, "certificate_mapping")

    assert found.looked_at == [MAPPING_PATH]
    assert found.guessed_because is not None
    assert "where Apchi would put one" in found.guessed_because


async def test_resource_groups_are_found_through_their_properties_file(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    fake_kubernetes.config_maps["trino-config"]["resource-groups-manager.properties"] = (
        "resource-groups.configuration-manager=file\n"
        "resource-groups.config-file=/etc/trino/theirs/groups.json\n"
    )
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"].append(
        {
            "name": "config",
            "mountPath": MANAGER_PATH,
            "subPath": "resource-groups-manager.properties",
        }
    )
    fake_kubernetes.secrets["their-groups"] = {
        "groups.json": (
            '{"rootGroups": [{"name": "etl", "hardConcurrencyLimit": 4}], '
            '"selectors": [{"group": "etl"}]}'
        )
    }
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"].append(
        {"name": "their-groups", "mountPath": "/etc/trino/theirs"}
    )
    fake_kubernetes.pod_spec["volumes"].append(
        {"name": "their-groups", "secret": {"secretName": "their-groups"}}
    )

    discovery = await discover(fake_kubernetes, settings)
    found = _of(discovery, "resource_groups")

    assert "/etc/trino/theirs/groups.json" in found.looked_at
    assert found.resources["etl"]["hard_concurrency_limit"] == 4
    assert [s["group"] for s in found.resources[SELECTORS]] == ["etl"]


async def test_client_certificates_are_listed_out_of_their_directory(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """A directory, not a file: the question is what is in here, and only the volume knows."""
    certificate, key = key_pair()
    fake_kubernetes.secrets[settings.client_certificate_secret_name] = {
        "finance.crt": certificate_pem(certificate).decode(),
        "finance.key": key_pem(key).decode(),
    }
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"].append(
        {"name": "client-certificates", "mountPath": MOUNT_DIR}
    )
    fake_kubernetes.pod_spec["volumes"].append(
        {
            "name": "client-certificates",
            "secret": {"secretName": settings.client_certificate_secret_name},
        }
    )

    discovery = await discover(fake_kubernetes, settings)
    found = _of(discovery, "client_certificates")

    assert found.looked_at == [MOUNT_DIR]
    assert set(found.resources) == {"finance"}
    assert found.resources["finance"]["certificate"].startswith("-----BEGIN CERTIFICATE")


async def test_an_event_listener_is_read_from_the_path_trino_fixes(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    fake_kubernetes.secrets[settings.event_listener_secret_name] = {
        "event-listener.properties": (
            "event-listener.name=http\n"
            "http-event-listener.connect-ingest-uri=http://collector:8080/e\n"
        )
    }
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"].append(
        {
            "name": "apchi-event-listener",
            "mountPath": LISTENER_PATH,
            "subPath": "event-listener.properties",
        }
    )
    fake_kubernetes.pod_spec["volumes"].append(
        {
            "name": "apchi-event-listener",
            "secret": {"secretName": settings.event_listener_secret_name},
        }
    )

    discovery = await discover(fake_kubernetes, settings)
    found = _of(discovery, "event_listeners")

    assert found.resources["http"]["properties"] == {
        "http-event-listener.connect-ingest-uri": "http://collector:8080/e"
    }


# --- what it refuses to hide --------------------------------------------------------


async def test_a_file_that_does_not_parse_is_reported_and_discovery_completes(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Failing would lose the thing discovery exists for: an Admin with one odd file could
    not see what Apchi understood, nor what to fix."""
    fake_kubernetes.secrets[settings.certificate_mapping_secret_name] = {
        "user-mapping.json": "this is not json"
    }

    discovery = await discover(fake_kubernetes, settings)
    found = _of(discovery, "certificate_mapping")

    assert found.resources == {}
    assert found.problems[0].kind == "unreadable"
    assert found.problems[0].path == MAPPING_PATH
    assert "not valid JSON" in found.problems[0].detail
    assert discovery.complete is False


async def test_something_apchi_cannot_express_is_reported(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Apchi holds one mapping pattern and a hand-written file may have several. The extras
    are what §13.3 will preserve as Admin values, so they must survive as something."""
    fake_kubernetes.secrets[settings.certificate_mapping_secret_name] = {
        "user-mapping.json": (
            '{"rules": [{"pattern": "CN=(.*?),.*"}, {"pattern": "^legacy_(.*)$"}]}'
        )
    }

    discovery = await discover(fake_kubernetes, settings)
    found = _of(discovery, "certificate_mapping")

    assert found.resources[MAPPING]["pattern"] == "CN=(.*?),.*"
    # `unaccounted` and nothing else. Apchi adds its own rules to every file it writes, so
    # the regenerated file is never byte-identical to a hand-written one -- what the lossy
    # check asks instead is whether Apchi's *model* survives a round trip, and here it does.
    # The second pattern is reported because Apchi cannot hold it, not because of the render.
    assert {p.kind for p in found.problems} == {"unaccounted"}
    assert "further pattern" in found.problems[0].detail


async def test_trino_told_to_read_a_file_nothing_mounts_is_reported(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The Cluster is already broken -- the coordinator will not come back from its next
    restart -- and an Admin should hear it from Apchi rather than from that restart."""
    fake_kubernetes.config_maps["trino-config"]["coordinator-config.properties"] = (
        "http-server.authentication.insecure.user-mapping.file=/etc/trino/nowhere/mapping.json\n"
    )

    discovery = await discover(fake_kubernetes, settings)
    found = _of(discovery, "certificate_mapping")

    assert [p.kind for p in found.problems] == ["unreadable"]
    assert "nothing mounts it" in found.problems[0].detail
    assert found.problems[0].path == "/etc/trino/nowhere/mapping.json"


async def test_a_comment_is_reported_as_unaccounted_rather_than_as_a_lossy_render(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """A comment is the simplest thing Apchi cannot write back, and it must be reported --
    unreported, the first Apply would delete it.

    But it is reported as content Apchi cannot hold, not as a render that loses something.
    The distinction is the whole reason the lossy check is a fixpoint rather than a
    comparison against the original: Apchi adds its own rules to every file, so comparing
    bytes flagged every hand-written file and said nothing.
    """
    fake_kubernetes.secrets[settings.event_listener_secret_name] = {
        "event-listener.properties": "# why this exists\nevent-listener.name=http\n"
    }
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"].append(
        {
            "name": "apchi-event-listener",
            "mountPath": LISTENER_PATH,
            "subPath": "event-listener.properties",
        }
    )
    fake_kubernetes.pod_spec["volumes"].append(
        {
            "name": "apchi-event-listener",
            "secret": {"secretName": settings.event_listener_secret_name},
        }
    )

    discovery = await discover(fake_kubernetes, settings)
    found = _of(discovery, "event_listeners")

    assert found.resources["http"]["type"] == "http"
    assert {p.kind for p in found.problems} == {"unaccounted"}
    assert "comment" in found.problems[0].detail


# --- the preconditions --------------------------------------------------------------


async def test_every_precondition_failure_is_reported_not_just_the_first(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """An Admin fixing a manifest should see the whole list, which is the rule the
    precondition checks already follow."""
    fake_kubernetes.pod_spec["initContainers"] = []
    fake_kubernetes.config_maps["trino-config"]["access-control.properties"] = (
        "access-control.name=file\n"
    )

    discovery = await discover(fake_kubernetes, settings)

    assert len(discovery.problems) >= 2
    assert "precondition" in _kinds(discovery)


async def test_the_seed_initcontainer_is_a_cutover_requirement_not_a_fault(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """It copies from a Secret Apchi has not written yet, so satisfying it before adoption
    means the next restart seeds an empty store and every catalog disappears. It is the one
    precondition the cutover owns, so it must not block the discovery."""
    fake_kubernetes.pod_spec["initContainers"] = []

    discovery = await discover(fake_kubernetes, settings)

    cutover = [p for p in discovery.problems if p.kind == "cutover"]
    assert len(cutover) == 1
    assert settings.catalog_secret_name in cutover[0].detail
    assert "precondition" not in {p.kind for p in discovery.problems}


async def test_a_cutover_requirement_alone_does_not_make_a_discovery_incomplete(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Completeness is about whether Apchi understood the configuration, not about whether
    the deployment has been changed yet -- that change comes after adoption by design."""
    fake_kubernetes.pod_spec["initContainers"] = []

    discovery = await discover(fake_kubernetes, settings)

    assert {p.kind for p in discovery.problems} == {"cutover"}
    # Still incomplete, but because no Trino was supplied to ask which catalogs are loaded
    # -- not because of the cutover.
    assert {p.kind for p in discovery.every_problem if p.kind != "cutover"} == {"unreadable"}


async def test_a_healthy_cluster_reports_no_cluster_level_problems(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    discovery = await discover(fake_kubernetes, settings)

    assert discovery.problems == []
    assert discovery.coordinator == settings.coordinator_deployment_name


# --- reading through whichever mount shape the Admin used ---------------------------


async def test_a_file_inside_a_whole_directory_mount_is_read(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """How the official Trino chart mounts configuration: one ConfigMap at /etc/trino rather
    than one mount per file. Apchi has to be able to read a Cluster shaped that way."""
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"] = [
        mount
        for mount in fake_kubernetes.pod_spec["containers"][0]["volumeMounts"]
        if mount["mountPath"] != "/etc/trino/access-control.properties"
    ]
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"].append(
        {"name": "config", "mountPath": "/etc/trino"}
    )

    discovery = await discover(fake_kubernetes, settings)

    # The refresh period is in that ConfigMap, so reading it through the directory mount is
    # what keeps the precondition satisfied.
    assert discovery.problems == []


async def test_certificates_are_not_listed_out_of_a_mount_that_only_contains_them(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """A directory inside a larger mount cannot be listed: its contents would be keys of the
    outer volume prefixed with a path a ConfigMap key cannot contain. Reporting nothing beats
    reporting a wrong something."""
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"] = [
        mount
        for mount in fake_kubernetes.pod_spec["containers"][0]["volumeMounts"]
        if mount["mountPath"] != MOUNT_DIR
    ]
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"].append(
        {"name": "config", "mountPath": "/etc/trino"}
    )
    fake_kubernetes.secrets[settings.client_certificate_secret_name] = {
        certificate_path("x").rsplit("/", 1)[1]: "cert",
        key_path("x").rsplit("/", 1)[1]: "key",
    }

    discovery = await discover(fake_kubernetes, settings)

    assert _of(discovery, "client_certificates").resources == {}


async def test_rules_named_by_the_properties_file_but_mounted_nowhere_are_reported(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Trino reads whatever `config-file` names. If that file is not there the coordinator
    will not come back from its next restart, and Apchi should be the one to say so."""
    fake_kubernetes.config_maps["trino-config"]["resource-groups-manager.properties"] = (
        "resource-groups.configuration-manager=file\n"
        "resource-groups.config-file=/etc/trino/gone.json\n"
    )
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"].append(
        {
            "name": "config",
            "mountPath": MANAGER_PATH,
            "subPath": "resource-groups-manager.properties",
        }
    )

    discovery = await discover(fake_kubernetes, settings)
    found = _of(discovery, "resource_groups")

    assert found.resources == {}
    # Two true reports: the rules Trino names are not there, and the properties file that
    # names them is the Admin's rather than Apchi's. Both matter to whoever fixes this.
    assert [(problem.kind, problem.path) for problem in found.problems] == [
        ("unreadable", "/etc/trino/gone.json"),
        ("unaccounted", MANAGER_PATH),
    ]
    assert "nothing mounts it" in found.problems[0].detail
    assert "not there" in found.problems[1].detail


async def test_a_properties_file_that_is_not_apchis_is_reported_alongside_the_rules(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """The rules are readable and are the ones in force, but the file pointing at them is
    the Admin's rather than Apchi's -- so after the cutover Trino would be reading a
    different file. Worth saying while the rules themselves import cleanly."""
    fake_kubernetes.config_maps["trino-config"]["resource-groups-manager.properties"] = (
        "resource-groups.configuration-manager=file\n"
        "resource-groups.config-file=/etc/trino/theirs/groups.json\n"
    )
    fake_kubernetes.secrets["their-groups"] = {
        "groups.json": (
            '{"rootGroups": [{"name": "g", "hardConcurrencyLimit": 1}], "selectors": []}'
        )
    }
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"] += [
        {
            "name": "config",
            "mountPath": MANAGER_PATH,
            "subPath": "resource-groups-manager.properties",
        },
        {"name": "their-groups", "mountPath": "/etc/trino/theirs"},
    ]
    fake_kubernetes.pod_spec["volumes"].append(
        {"name": "their-groups", "secret": {"secretName": "their-groups"}}
    )

    discovery = await discover(fake_kubernetes, settings)
    found = _of(discovery, "resource_groups")

    assert found.resources["g"]["hard_concurrency_limit"] == 1
    assert any("not the one Apchi writes" in p.detail for p in found.problems)

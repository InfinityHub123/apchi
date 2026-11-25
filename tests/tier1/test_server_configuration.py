"""Trino's own files: the Admin's properties, rendered with the chart's wiring (ADR-0010).

What tier 1 can prove, against a faked Kubernetes and a real MongoDB and Trino:

* **the contract** -- what is stored, what is refused before anything is stored, and who may
  touch it at all
* **the rendering** -- wiring first and exactly as the settings say, then the Admin's keys
* **the pipeline** -- delivered on every Apply, a restart exactly when the rendered files
  differ from the coordinator's, the probe given the Admin's properties and never sizing, and
  the previous properties restored -- with a restart -- when an Admin Apply fails
* **the first write** -- written when absent, never rewritten when present

What it cannot prove is that Trino reads any of it, or refuses a bad property: the probe here
is a container started before the test. That is tier 2's, against a real cluster.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.trino import Trino
from app.api.deps import caller
from app.auth.identity import Identity
from app.config import AuthMode, Settings
from app.main import create_app
from app.pipeline.applies import TERMINAL
from app.pipeline.configuration import FORMAT
from app.sections.admin import AdminValues
from app.sections.registry import SECTIONS
from app.sections.server_configuration import (
    ACCESS_CONTROL_PATH,
    CATALOG_STORE_PROPERTIES,
    CONFIG_PROPERTIES,
    JVM_CONFIG,
    LOG_PROPERTIES,
    NODE_PROPERTIES,
    REFRESH_PERIOD,
    RULES_PATH,
    USER_MAPPING_PATH,
    WIRING,
    ServerConfiguration,
    coordinator_secret,
    render_coordinator,
)
from tests.conftest import SERVER_CONFIG_SECRET, FakeKubernetes

SERVER_CONFIGURATION = "/api/v1/admin/server-configuration"
RENDERED = f"{SERVER_CONFIGURATION}/files"
ADMIN_APPLIES = "/api/v1/admin/applies"
APPLIES = "/api/v1/applies"
FILES = "/api/v1/admin/files"

ADMIN = Identity(subject="root", role="admin", via="password")
OPERATOR = Identity(subject="jo", role="operator", via="password")

SHARED_SECRET = "s3cret-internal-communication"
PROPERTIES = {
    "config.properties": {
        "query.max-execution-time": "2h",
        "internal-communication.shared-secret": SHARED_SECRET,
    },
    "log.properties": {"io.trino.plugin.postgresql": "DEBUG"},
}


def _body(coordinator: dict[str, dict[str, str]]) -> dict[str, Any]:
    return {"coordinator": coordinator}


async def _put(http: AsyncClient, coordinator: dict[str, dict[str, str]]) -> Any:
    return await http.put(SERVER_CONFIGURATION, json=_body(coordinator))


async def _apply(http: AsyncClient, path: str = ADMIN_APPLIES, timeout: float = 180.0) -> dict:
    started = await http.post(path, json={})
    assert started.status_code == 202, started.text
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await http.get(f"{APPLIES}/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return dict(record)
        await asyncio.sleep(0.05)
    raise AssertionError("the Apply never settled")


async def _serving(
    settings: Settings, fake: FakeKubernetes, who: Identity
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings.model_copy(update={"auth_mode": AuthMode.PASSWORD}))
    app.dependency_overrides[caller] = lambda: who
    app.state.kubernetes = fake
    app.state.trino = Trino(url="http://127.0.0.1:1")
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="https://apchi") as http,
        app.router.lifespan_context(app),
    ):
        yield http


@pytest.fixture
async def as_admin(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    async for http in _serving(settings, fake_kubernetes, ADMIN):
        yield http


@pytest.fixture
async def as_operator(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> AsyncIterator[AsyncClient]:
    async for http in _serving(settings, fake_kubernetes, OPERATOR):
        yield http


# --- the contract -----------------------------------------------------------------------------


async def test_nothing_is_set_until_an_admin_sets_it(as_admin: AsyncClient) -> None:
    response = await as_admin.get(SERVER_CONFIGURATION)

    assert response.status_code == 200, response.text
    assert response.json() == _body(
        {"config.properties": {}, "node.properties": {}, "log.properties": {}}
    )


async def test_properties_are_stored_by_the_file_trino_reads_them_from(
    as_admin: AsyncClient,
) -> None:
    """Keyed by the file name, dots and all, because that is what an Admin knows the property
    by -- the name of the file it goes in."""
    assert (await _put(as_admin, PROPERTIES)).status_code == 200

    stored = (await as_admin.get(SERVER_CONFIGURATION)).json()["coordinator"]

    assert stored["config.properties"] == PROPERTIES["config.properties"]
    assert stored["log.properties"] == PROPERTIES["log.properties"]
    assert stored["node.properties"] == {}


async def test_a_put_replaces_every_property_rather_than_merging(as_admin: AsyncClient) -> None:
    await _put(as_admin, PROPERTIES)
    await _put(as_admin, {"config.properties": {"query.max-run-time": "4h"}})

    stored = (await as_admin.get(SERVER_CONFIGURATION)).json()["coordinator"]

    assert stored["config.properties"] == {"query.max-run-time": "4h"}
    assert stored["log.properties"] == {}


@pytest.mark.parametrize(
    ("file", "key"),
    [(file, key) for file, keys in WIRING.items() for key in keys],
)
async def test_wiring_is_refused_and_names_what_owns_it(
    as_admin: AsyncClient, file: str, key: str
) -> None:
    """Every key the chart owns, one by one: a list checked only by example is a list one entry
    of which nobody has ever seen refused."""
    response = await _put(as_admin, {file: {key: "anything"}})

    assert response.status_code == 422, response.text
    assert f"{key} is wiring" in response.text
    assert WIRING[file][key] in response.text
    assert (await as_admin.get(SERVER_CONFIGURATION)).json()["coordinator"][file] == {}


@pytest.mark.parametrize(
    ("key", "value", "why"),
    [
        ("query.max-execution-time", "2h\ncoordinator=false", "line break"),
        ("query.max-execution-time", "2h\rcoordinator=false", "line break"),
        ("query.max-execution-time", "2h\\", "joins the next line"),
        ("query.max-execution-time", "2h\\\\\\", "joins the next line"),
        ("query.max\nexecution", "2h", "not a property name"),
        ("query.max-execution-time=3h", "2h", "not a property name"),
        ("query:max", "2h", "not a property name"),
        ("query max", "2h", "not a property name"),
        ("#query.max-execution-time", "2h", "not a property name"),
        ("!query.max-execution-time", "2h", "not a property name"),
        ("query\\max", "2h", "not a property name"),
        ("", "2h", "cannot be empty"),
        ("query.max-execution-time", "x" * (16 * 1024 + 1), "longer than"),
    ],
)
async def test_what_trino_would_read_differently_from_how_it_is_written_is_refused(
    as_admin: AsyncClient, key: str, value: str, why: str
) -> None:
    """Each of these would make the file Trino reads differ from the map the Admin wrote. The
    line break matters most: it adds a line of its own, and that line can be wiring."""
    response = await _put(as_admin, {"config.properties": {key: value}})

    assert response.status_code == 422, response.text
    assert why in response.text
    assert (await as_admin.get(SERVER_CONFIGURATION)).json()["coordinator"][
        "config.properties"
    ] == {}


@pytest.mark.parametrize(
    "value",
    [
        "jdbc:postgresql://db:5432/x?a=b",  # '=' and ':' are fine in a value
        "C:\\\\path",  # an escaped backslash, not a continuation
        "",
        "${ENV:SOME_VARIABLE}",
    ],
)
async def test_values_with_properties_syntax_in_them_are_accepted(
    as_admin: AsyncClient, value: str
) -> None:
    response = await _put(as_admin, {"config.properties": {"some.property": value}})

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "file",
    ["jvm.config", "catalog-store.properties", "access-control.properties", "extra.properties"],
)
async def test_files_that_are_not_the_admins_are_refused(as_admin: AsyncClient, file: str) -> None:
    """jvm.config is the chart's -- sizing travels in JDK_JAVA_OPTIONS -- and the catalog store
    and access-control files are wiring through and through. Anything else is a Raw File."""
    response = await _put(as_admin, {file: {"x": "y"}})

    assert response.status_code == 422, response.text


async def test_the_workers_are_not_yet_part_of_it(as_admin: AsyncClient) -> None:
    """Their files are still the chart's in this stage. Accepting worker properties that
    reach no worker would be a setting that silently does nothing."""
    response = await as_admin.put(
        SERVER_CONFIGURATION, json={"worker": {"config.properties": {"task.concurrency": "8"}}}
    )

    assert response.status_code == 422, response.text


async def test_a_value_that_is_not_a_string_is_refused(as_admin: AsyncClient) -> None:
    response = await _put(as_admin, {"config.properties": {"query.max-queued-queries": 500}})  # type: ignore[dict-item]

    assert response.status_code == 422, response.text


async def test_a_file_with_too_many_properties_is_refused(as_admin: AsyncClient) -> None:
    response = await _put(
        as_admin, {"log.properties": {f"io.trino.x{i}": "DEBUG" for i in range(501)}}
    )

    assert response.status_code == 422, response.text
    assert "more than 500" in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", SERVER_CONFIGURATION), ("PUT", SERVER_CONFIGURATION), ("GET", RENDERED)],
)
async def test_an_operator_cannot_read_or_write_it(
    as_operator: AsyncClient, method: str, path: str
) -> None:
    """The properties can be credentials, and sizing a platform is the Admin's decision."""
    response = await as_operator.request(method, path, json=_body({}))

    assert response.status_code == 403, response.text


async def test_the_audit_records_that_it_changed_and_never_a_value(as_admin: AsyncClient) -> None:
    """A value may be a credential -- the shared secret here is one."""
    await _put(as_admin, PROPERTIES)

    page = (await as_admin.get("/api/v1/audit", params={"category": "admin", "limit": 50})).json()

    events = [event for event in page["items"] if event["resource"] == "server_configuration"]
    assert len(events) == 1
    assert SHARED_SECRET not in str(page)
    assert "2h" not in str(events)


async def test_a_value_is_never_logged(
    as_admin: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    await _put(as_admin, PROPERTIES)

    assert SHARED_SECRET not in caplog.text
    assert "server configuration" in caplog.text


# --- the rendering ----------------------------------------------------------------------------


async def test_the_rendered_files_are_wiring_then_the_admins_keys(as_admin: AsyncClient) -> None:
    """Exactly, line for line: a rendered file is what Trino reads, and a line in the wrong
    place -- an Admin key before the wiring it may not override -- is the bug this guards."""
    await _put(as_admin, PROPERTIES)

    files = (await as_admin.get(RENDERED)).json()["coordinator"]

    assert files[CONFIG_PROPERTIES] == (
        "coordinator=true\n"
        "http-server.http.port=8080\n"
        "discovery.uri=http://trino:8080\n"
        "catalog.management=dynamic\n"
        "catalog.store=file\n"
        f"http-server.authentication.insecure.user-mapping.file={USER_MAPPING_PATH}\n"
        "query.max-memory=${ENV:TRINO_QUERY_MAX_MEMORY}\n"
        "query.max-memory-per-node=${ENV:TRINO_QUERY_MAX_MEMORY_PER_NODE}\n"
        "memory.heap-headroom-per-node=${ENV:TRINO_MEMORY_HEAP_HEADROOM_PER_NODE}\n"
        f"internal-communication.shared-secret={SHARED_SECRET}\n"
        "node-scheduler.include-coordinator=false\n"
        "query.max-execution-time=2h\n"
    )
    assert files[LOG_PROPERTIES] == "io.trino.plugin.postgresql=DEBUG\n"
    assert files[NODE_PROPERTIES] == "node.environment=production\nnode.data-dir=/data/trino\n"
    assert files[JVM_CONFIG] == "-Djdk.attach.allowAttachSelf=true\n"
    assert files[CATALOG_STORE_PROPERTIES] == "catalog.config-dir=/data/trino/catalogs\n"
    assert files[ACCESS_CONTROL_PATH] == (
        f"access-control.name=file\nsecurity.config-file={RULES_PATH}\n{REFRESH_PERIOD}=30s\n"
    )
    assert set(files) == {
        CONFIG_PROPERTIES,
        LOG_PROPERTIES,
        NODE_PROPERTIES,
        JVM_CONFIG,
        CATALOG_STORE_PROPERTIES,
        ACCESS_CONTROL_PATH,
    }


async def test_an_admin_overrides_a_default_but_never_wiring(as_admin: AsyncClient) -> None:
    """`node-scheduler.include-coordinator` is a default, not wiring: a small Cluster may want
    its coordinator to work too."""
    await _put(as_admin, {"config.properties": {"node-scheduler.include-coordinator": "true"}})

    rendered = (await as_admin.get(RENDERED)).json()["coordinator"][CONFIG_PROPERTIES]

    assert "node-scheduler.include-coordinator=true\n" in rendered
    assert "node-scheduler.include-coordinator=false" not in rendered


def test_the_heap_is_never_in_jvm_config() -> None:
    """Measured on Trino 483: jvm.config is read after JDK_JAVA_OPTIONS and its last -Xmx wins,
    so a heap flag here would silently override the chart's sizing."""
    rendered = render_coordinator(ServerConfiguration(), Settings(_env_file=None))  # type: ignore[call-arg]

    assert "Xmx" not in rendered[JVM_CONFIG]
    assert "RAMPercentage" not in rendered[JVM_CONFIG]


@pytest.mark.parametrize(
    ("update", "expected", "absent"),
    [
        (
            {"trino_https_port": 8443},
            [
                "http-server.https.enabled=true",
                "http-server.https.port=8443",
                "http-server.https.keystore.path=/data/trino-https/keystore.pem",
            ],
            [],
        ),
        ({"trino_process_forwarded": True}, ["http-server.process-forwarded=true"], []),
        (
            {"trino_authentication_type": "certificate"},
            [f"http-server.authentication.certificate.user-mapping.file={USER_MAPPING_PATH}"],
            ["insecure.user-mapping", "http-server.authentication.type"],
        ),
        (
            {"trino_http_port": 9090, "trino_discovery_uri": "http://trino-a:9090"},
            ["http-server.http.port=9090", "discovery.uri=http://trino-a:9090"],
            ["8080"],
        ),
        ({}, [], ["https", "process-forwarded"]),
    ],
)
def test_wiring_follows_the_settings_the_chart_passes(
    update: dict[str, Any], expected: list[str], absent: list[str]
) -> None:
    settings = Settings(_env_file=None).model_copy(update=update)  # type: ignore[call-arg]

    rendered = render_coordinator(ServerConfiguration(), settings)[CONFIG_PROPERTIES]

    for line in expected:
        assert f"{line}\n" in rendered
    for fragment in absent:
        assert fragment not in rendered


def test_a_truststore_reaches_the_jvm_and_nothing_else_does() -> None:
    settings = Settings(_env_file=None).model_copy(  # type: ignore[call-arg]
        update={"trino_java_truststore_path": "/etc/pki/java/truststore/cacerts"}
    )

    jvm = render_coordinator(ServerConfiguration(), settings)[JVM_CONFIG]

    assert jvm == (
        "-Djdk.attach.allowAttachSelf=true\n"
        "-Djavax.net.ssl.trustStore=/etc/pki/java/truststore/cacerts\n"
    )


def test_the_environment_and_refresh_period_come_from_the_settings() -> None:
    settings = Settings(_env_file=None).model_copy(  # type: ignore[call-arg]
        update={"trino_environment": "apchidev", "trino_security_refresh_period": "1s"}
    )

    rendered = render_coordinator(ServerConfiguration(), settings)

    assert "node.environment=apchidev\n" in rendered[NODE_PROPERTIES]
    assert f"{REFRESH_PERIOD}=1s\n" in rendered[ACCESS_CONTROL_PATH]


@pytest.mark.parametrize(
    ("variable", "value", "why"),
    [
        ("APCHI_TRINO_SECURITY_REFRESH_PERIOD", " ", "trino_security_refresh_period is required"),
        ("APCHI_TRINO_ENVIRONMENT", "Prod-1", "lowercase letters, digits and underscores"),
        ("APCHI_TRINO_AUTHENTICATION_TYPE", "password", "insecure"),
    ],
)
def test_wiring_trino_would_refuse_stops_apchi_starting(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str, why: str
) -> None:
    """Refused where the setting is named, rather than in a coordinator that will not start
    after the next Apply."""
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValueError, match=why):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_the_paths_agree_with_the_sections_that_own_them() -> None:
    """Spelled out in the module to avoid an import cycle, so held equal here instead."""
    from app.sections.certificate_mapping.generator import MOUNT_PATH as mapping
    from app.sections.permissions.generator import MOUNT_PATH as rules
    from app.sections.permissions.generator import PROPERTIES_PATH
    from app.sections.permissions.generator import REFRESH_PERIOD as refresh

    assert (
        mapping,
        rules,
        PROPERTIES_PATH,
        refresh,
    ) == (USER_MAPPING_PATH, RULES_PATH, ACCESS_CONTROL_PATH, REFRESH_PERIOD)


# --- the pipeline -----------------------------------------------------------------------------


async def test_an_admin_apply_delivers_it_and_restarts_the_coordinator(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    await _put(applying_client, PROPERTIES)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    assert record["snapshot"] is None, "an Admin Apply creates no Snapshot (§14)"
    expected = coordinator_secret(
        AdminValues.model_validate(
            {"server_configuration": _body(PROPERTIES)}
        ).server_configuration,
        settings,
    )
    assert fake_kubernetes.secrets[SERVER_CONFIG_SECRET] == expected
    assert len(fake_kubernetes.restarts) == 1
    assert "server configuration" in fake_kubernetes.restarts[0]


async def test_applying_it_again_restarts_nothing(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Every Apply renders these files, so a restart has to be decided by whether they
    differ from what the coordinator holds -- or every Apply would end every query."""
    await _put(applying_client, PROPERTIES)
    await _apply(applying_client)

    second = await _apply(applying_client)

    assert second["stage"] == "succeeded", second
    assert len(fake_kubernetes.restarts) == 1


async def test_an_operator_apply_of_an_unrelated_change_restarts_nothing(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    record = await _apply(applying_client, APPLIES)

    assert record["stage"] == "succeeded", record
    assert fake_kubernetes.restarts == []


async def test_an_unapplied_admin_change_is_delivered_by_the_next_apply_whoever_starts_it(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Every Apply renders Trino's files from the Admin values in force -- the same rule Raw
    Files follow -- so Review has to say so before an Operator commits to the restart."""
    await _put(applying_client, PROPERTIES)

    review = (await applying_client.get("/api/v1/review")).json()
    assert review["cost"]["restarts_coordinator"] is True

    record = await _apply(applying_client, APPLIES)

    assert record["stage"] == "succeeded", record
    assert len(fake_kubernetes.restarts) == 1
    assert (
        "query.max-execution-time=2h"
        in (fake_kubernetes.secrets[SERVER_CONFIG_SECRET]["config.properties"])
    )
    after = (await applying_client.get("/api/v1/review")).json()
    assert after["cost"]["restarts_coordinator"] is False


async def test_wiring_the_chart_changed_shows_as_a_restart_and_the_next_apply_delivers_it(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """What the coordinator holds was rendered with the chart's old wiring -- here, by hand.
    Apchi never rewrites it at startup, so Review is where the difference shows."""
    stale = dict(fake_kubernetes.secrets[SERVER_CONFIG_SECRET])
    stale["node.properties"] = "node.environment=old\nnode.data-dir=/data/trino\n"
    fake_kubernetes.secrets[SERVER_CONFIG_SECRET] = stale

    assert (await applying_client.get("/api/v1/review")).json()["cost"]["restarts_coordinator"]

    await _apply(applying_client, APPLIES)

    assert (
        "node.environment=production"
        in (fake_kubernetes.secrets[SERVER_CONFIG_SECRET]["node.properties"])
    )
    assert len(fake_kubernetes.restarts) == 1


# --- the probe --------------------------------------------------------------------------------


def _probe_files(fake: FakeKubernetes) -> dict[str, str]:
    """What the most recent validation probe was given, by file name."""
    assert fake.pod_history, "no validation probe was started"
    return fake.secret_history[fake.pod_history[-1]]


async def test_an_admin_change_alone_is_validated_on_the_probe(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """With nothing in any Section there is nothing else to probe -- and before this stage, an
    Admin Apply on such a Cluster went to the coordinator unvalidated."""
    await _put(applying_client, PROPERTIES)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    files = _probe_files(fake_kubernetes)
    assert "query.max-execution-time=2h\n" in files["config.properties"]
    assert files["log.properties"] == "io.trino.plugin.postgresql=DEBUG\n"


async def test_the_probe_is_given_no_sizing(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The probe has none of the sizing variables, and a reference to one stops Trino with
    "Environment variable is not set" (measured). Sizing is proved on the Cluster."""
    await _put(applying_client, PROPERTIES)

    await _apply(applying_client)

    assert "${ENV:TRINO_" not in str(_probe_files(fake_kubernetes))


async def test_the_probes_own_wiring_wins_over_the_admins(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The probe has no workers: a coordinator told not to schedule work would still boot, but
    the probe's wiring is what lets it read the files under test, so it is laid over the top."""
    await _put(
        applying_client,
        {
            "config.properties": {"node-scheduler.include-coordinator": "false"},
            "node.properties": {"node.internal-address-source": "FQDN"},
        },
    )

    await _apply(applying_client)

    files = _probe_files(fake_kubernetes)
    assert "node-scheduler.include-coordinator=true\n" in files["config.properties"]
    assert "node-scheduler.include-coordinator=false" not in files["config.properties"]
    assert files["node.properties"] == (
        "node.internal-address-source=FQDN\nnode.environment=apchi_probe\nnode.data-dir=/data/trino\n"
    )


async def test_nothing_changed_means_no_probe(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await _put(applying_client, PROPERTIES)
    await _apply(applying_client)
    probes = len(fake_kubernetes.pod_history)

    await _apply(applying_client)

    assert len(fake_kubernetes.pod_history) == probes


async def test_a_failed_admin_apply_puts_the_previous_properties_back_and_restarts_again(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """There is no Snapshot to restore Admin values from (invariant 9); the pre-image is the
    record. And the coordinator reads these files once at start, so putting them back without
    a second restart would leave it running what failed."""
    await _put(applying_client, {"config.properties": {"query.max-run-time": "4h"}})
    await _apply(applying_client)
    good = dict(fake_kubernetes.secrets[SERVER_CONFIG_SECRET])
    await _put(applying_client, {"config.properties": {"query.max-run-time": "1ms"}})
    fake_kubernetes.report_a_missing_worker_once = True

    record = await _apply(applying_client)

    assert record["stage"] == "failed", record
    assert fake_kubernetes.secrets[SERVER_CONFIG_SECRET] == good
    stored = (await applying_client.get(SERVER_CONFIGURATION)).json()["coordinator"]
    assert stored["config.properties"] == {"query.max-run-time": "4h"}
    assert len(fake_kubernetes.restarts) == 3, fake_kubernetes.restarts
    assert "auto rollback" in fake_kubernetes.restarts[-1]


async def test_a_mount_under_the_config_directory_that_is_not_apchis_refuses_the_apply(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The ninth precondition, through the API: a chart that mounts a file of its own under
    /etc/trino again is refused before anything is written."""
    fake_kubernetes.pod_spec["containers"][0]["volumeMounts"].append(
        {"name": "chart", "mountPath": "/etc/trino/exchange-manager.properties", "subPath": "x"}
    )
    fake_kubernetes.pod_spec["volumes"].append({"name": "chart", "configMap": {"name": "c"}})

    record = await _apply(applying_client, APPLIES)

    assert record["stage"] == "failed", record
    assert "/etc/trino/exchange-manager.properties" in record["failure_reason"]
    assert "ConfigMap 'c'" in record["failure_reason"]
    assert fake_kubernetes.restarts == []


async def test_a_server_configuration_mounted_world_readable_refuses_the_apply(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """The Admin's properties can be credentials, so the seventh precondition holds this
    Secret to the mode it holds every other credential to."""
    for volume in fake_kubernetes.pod_spec["volumes"]:
        if volume["name"] == "server-config":
            volume["secret"]["defaultMode"] = 0o644

    record = await _apply(applying_client, APPLIES)

    assert record["stage"] == "failed", record
    assert "'server-config' volume holds credentials" in record["failure_reason"]


# --- the first write --------------------------------------------------------------------------


async def _settled_write(fake: FakeKubernetes, timeout: float = 5.0) -> dict[str, str]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if SERVER_CONFIG_SECRET in fake.secrets:
            return fake.secrets[SERVER_CONFIG_SECRET]
        await asyncio.sleep(0.02)
    raise AssertionError("the server configuration was never written")


async def test_apchi_writes_it_on_start_when_it_does_not_exist(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """A new install: the coordinator waits in ContainerCreating for this Secret."""
    del fake_kubernetes.secrets[SERVER_CONFIG_SECRET]

    async for _ in _serving(settings, fake_kubernetes, ADMIN):
        written = await _settled_write(fake_kubernetes)

    assert written == coordinator_secret(AdminValues().server_configuration, settings)


async def test_the_first_write_carries_the_admin_values_already_stored(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """A Secret deleted on a Cluster that already has Admin values comes back with them, not
    with the defaults."""
    async for http in _serving(settings, fake_kubernetes, ADMIN):
        await _put(http, PROPERTIES)
    del fake_kubernetes.secrets[SERVER_CONFIG_SECRET]

    async for _ in _serving(settings, fake_kubernetes, ADMIN):
        written = await _settled_write(fake_kubernetes)

    assert "query.max-execution-time=2h\n" in written["config.properties"]


async def test_apchi_never_rewrites_it_on_start(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """What is there is what the running coordinator started with. Restarting Apchi must never
    end a query, so a difference waits for the next Apply."""
    held = {**fake_kubernetes.secrets[SERVER_CONFIG_SECRET], "node.properties": "held\n"}
    fake_kubernetes.secrets[SERVER_CONFIG_SECRET] = held

    async for http in _serving(settings, fake_kubernetes, ADMIN):
        await asyncio.sleep(0.3)
        assert (await http.get("/api/v1/health/live")).status_code in (200, 404)

    assert fake_kubernetes.secrets[SERVER_CONFIG_SECRET] == held


async def test_the_first_write_is_retried_until_it_lands(
    settings: Settings, fake_kubernetes: FakeKubernetes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a new install MongoDB or the API server may not answer yet, and a coordinator waiting
    on a write that was tried once and given up on would wait until somebody restarted Apchi."""
    del fake_kubernetes.secrets[SERVER_CONFIG_SECRET]
    monkeypatch.setattr("app.main._FIRST_WRITE_RETRY_SECONDS", 0.01)
    real = fake_kubernetes.read_secret_if_present
    failures = {"left": 3}

    async def flaky(name: str) -> dict[str, str] | None:
        if name == SERVER_CONFIG_SECRET and failures["left"]:
            failures["left"] -= 1
            raise ConnectionError("the API server is not answering yet")
        return await real(name)

    monkeypatch.setattr(fake_kubernetes, "read_secret_if_present", flaky)

    async for _ in _serving(settings, fake_kubernetes, ADMIN):
        written = await _settled_write(fake_kubernetes)

    assert failures["left"] == 0
    assert written == coordinator_secret(AdminValues().server_configuration, settings)


# --- export and import ------------------------------------------------------------------------


def _admin_document(admin: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "sections": {name: {} for name in SECTIONS},
        "admin": admin,
    }


async def test_the_full_export_carries_it_and_imports_unedited(as_admin: AsyncClient) -> None:
    await _put(as_admin, PROPERTIES)
    exported = (await as_admin.get("/api/v1/admin/configuration/export")).json()

    assert (
        exported["admin"]["server_configuration"]["coordinator"]["config.properties"]
        == (PROPERTIES["config.properties"])
    )
    await _put(as_admin, {})
    response = await as_admin.put("/api/v1/admin/configuration", json=exported)

    assert response.status_code == 200, response.text
    assert (await as_admin.get(SERVER_CONFIGURATION)).json()["coordinator"][
        "config.properties"
    ] == PROPERTIES["config.properties"]


async def test_an_import_with_wiring_in_it_is_refused(as_admin: AsyncClient) -> None:
    document = _admin_document(
        {"server_configuration": {"coordinator": {"config.properties": {"discovery.uri": "x"}}}}
    )

    response = await as_admin.put("/api/v1/admin/configuration", json=document)

    assert response.status_code == 422, response.text
    assert "discovery.uri is wiring" in response.text


@pytest.mark.parametrize(
    "path", [CONFIG_PROPERTIES, JVM_CONFIG, "/etc/trino/./log.properties", ACCESS_CONTROL_PATH]
)
async def test_an_import_with_a_raw_file_apchi_renders_is_refused(
    as_admin: AsyncClient, path: str
) -> None:
    """The same rule writing one through /admin/files follows, so an import cannot store what
    no Apply would run."""
    document = _admin_document({"raw_files": {path: "x=y\n"}})

    response = await as_admin.put("/api/v1/admin/configuration", json=document)

    assert response.status_code == 422, response.text
    assert "renders itself" in response.text
    assert (await as_admin.get(FILES)).json() == []


@pytest.mark.parametrize(
    ("log", "reason"),
    [
        (
            "2026-11-25T07:25:11.539Z\tERROR\tmain\tio.trino.server.Server\tConfiguration is "
            "invalid\n==========\n\nErrors:\n\n1) Configuration property 'query.max-memroy' was "
            "not used. Did you mean to use 'query.max-memory'?\n\n==========\n"
            "2026-11-25T07:25:11.600Z\tINFO\tmain\tio.trino.bootstrap.engine\tquery.max-memory\n",
            "1) Configuration property 'query.max-memroy' was not used",
        ),
        (
            "ERROR\tmain\tio.trino.server.Server\tConfiguration is invalid\nErrors:\n\n1) Invalid "
            "value '2hours' for type io.airlift.units.Duration (property "
            "'query.max-execution-time')\n",
            "1) Invalid value '2hours'",
        ),
    ],
)
def test_the_probe_quotes_the_line_trino_gives_its_reason_on(log: str, reason: str) -> None:
    """Measured on Trino 483: the reason is on a numbered line carrying none of the markers the
    excerpt used to look for, so an Admin was told only "Configuration is invalid"."""
    from app.pipeline.validation import _why

    assert reason in _why(log)

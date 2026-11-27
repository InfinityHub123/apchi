"""The Event Listener as the Admin's: `event-listener.properties` in the coordinator's Server
Configuration (#269).

It was an Operator Section, delivered by Apchi adding and removing a mount on the coordinator
Deployment -- the chart's object, which Argo CD reconciles. Now it is one more file in the
coordinator's Secret, which the chart mounts whole at a fixed directory, and config.properties
points Trino at it only when there is one. Nothing is mounted or unmounted for it.

Measured on Trino 483, which is what this rests on: `event-listener.config-files` naming a
file in a subdirectory loads it; a broken listener there stops startup with numbered reasons;
and with that property set, a file at Trino's default path is ignored.
"""

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient

from app.config import Settings
from app.pipeline.applies import TERMINAL
from app.pipeline.preconditions import PreconditionFailed, check, pod_spec
from app.sections.server_configuration import (
    CONFIG_PROPERTIES,
    LISTENER_DIR,
    LISTENER_FILE,
    LISTENER_FILES_KEY,
    LISTENER_PATH,
    ServerConfiguration,
    coordinator_secret,
    render_coordinator,
)
from tests.conftest import SERVER_CONFIG_SECRET, FakeKubernetes, healthy_pod_spec

SERVER_CONFIGURATION = "/api/v1/admin/server-configuration"
ADMIN_APPLIES = "/api/v1/admin/applies"
APPLIES = "/api/v1/applies"
SNAPSHOTS = "/api/v1/snapshots"

HTTP = {
    "event-listener.name": "http",
    "http-event-listener.connect-ingest-uri": "http://collector:8080/events",
}
LINE = f"{LISTENER_FILES_KEY}={LISTENER_PATH}\n"


def _with(listener: dict[str, str]) -> ServerConfiguration:
    return ServerConfiguration.model_validate({"coordinator": {LISTENER_FILE: listener}})


async def _put(http: AsyncClient, coordinator: dict[str, Any]) -> Any:
    return await http.put(SERVER_CONFIGURATION, json={"coordinator": coordinator})


async def _apply(http: AsyncClient, timeout: float = 180.0) -> dict:
    started = await http.post(ADMIN_APPLIES, json={})
    assert started.status_code == 202, started.text
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await http.get(f"{APPLIES}/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return dict(record)
        await asyncio.sleep(0.05)
    raise AssertionError("the Apply never settled")


# --- rendering ---------------------------------------------------------------------------------


def test_no_listener_renders_neither_the_file_nor_the_pointer(settings: Settings) -> None:
    """The default: nothing names a file, so there is nothing missing for Trino to refuse."""
    rendered = render_coordinator(ServerConfiguration(), settings)

    assert LISTENER_PATH not in rendered
    assert LISTENER_FILES_KEY not in rendered[CONFIG_PROPERTIES]


def test_a_listener_renders_the_file_and_points_config_properties_at_it(
    settings: Settings,
) -> None:
    rendered = render_coordinator(_with(HTTP), settings)

    assert rendered[LISTENER_PATH] == (
        "event-listener.name=http\n"
        "http-event-listener.connect-ingest-uri=http://collector:8080/events\n"
    )
    assert LINE in rendered[CONFIG_PROPERTIES]


def test_the_file_is_in_the_coordinators_secret_under_its_own_name(settings: Settings) -> None:
    """The chart mounts that Secret whole at the directory, so the key is the file name."""
    secret = coordinator_secret(_with(HTTP), settings)

    assert f"{LISTENER_DIR}/{LISTENER_FILE}" == LISTENER_PATH
    assert secret[LISTENER_FILE].startswith("event-listener.name=http\n")


def test_the_plugin_name_comes_first_and_the_rest_sorted(settings: Settings) -> None:
    """Stable, so Review diffs it by what changed rather than by the order it was typed."""
    listener = {
        "http-event-listener.log-completed": "true",
        "event-listener.name": "http",
        "http-event-listener.connect-ingest-uri": "http://c:8080/e",
    }

    lines = render_coordinator(_with(listener), settings)[LISTENER_PATH].splitlines()

    assert lines == [
        "event-listener.name=http",
        "http-event-listener.connect-ingest-uri=http://c:8080/e",
        "http-event-listener.log-completed=true",
    ]


# --- what the Admin may write ----------------------------------------------------------------


async def test_it_is_set_and_read_back_through_the_server_configuration(
    client: AsyncClient,
) -> None:
    written = await _put(client, {LISTENER_FILE: HTTP})

    assert written.status_code == 200, written.text
    read = (await client.get(SERVER_CONFIGURATION)).json()
    assert read["coordinator"][LISTENER_FILE] == HTTP
    files = (await client.get(f"{SERVER_CONFIGURATION}/files")).json()
    assert files["coordinator"][LISTENER_PATH].startswith("event-listener.name=http\n")
    assert LINE in files["coordinator"][CONFIG_PROPERTIES]
    assert LISTENER_PATH not in files["worker"], "a worker sends no query events"


async def test_a_listener_without_its_plugin_name_is_refused(client: AsyncClient) -> None:
    """Trino refuses that file at startup; it is cheaper to say so here."""
    refused = await _put(client, {LISTENER_FILE: {"http-event-listener.log-completed": "true"}})

    assert refused.status_code == 422
    assert "event-listener.name is required" in refused.text


async def test_a_curated_listener_missing_a_required_property_is_refused(
    client: AsyncClient,
) -> None:
    """The curated schemas the Section used still apply: `http` needs somewhere to send to."""
    refused = await _put(client, {LISTENER_FILE: {"event-listener.name": "http"}})

    assert refused.status_code == 422
    assert "connect-ingest-uri" in refused.text


async def test_a_listener_apchi_has_no_schema_for_is_left_to_trino(client: AsyncClient) -> None:
    """Judged on the validation probe instead, where Trino says what it thinks of it."""
    accepted = await _put(
        client, {LISTENER_FILE: {"event-listener.name": "acme", "acme.endpoint": "x"}}
    )

    assert accepted.status_code == 200, accepted.text


@pytest.mark.parametrize(
    ("key", "value", "problem"),
    [
        ("http-event-listener.connect-ingest-uri", "http://c\nevent-listener.name=x", "line break"),
        ("http-event-listener.connect-ingest-uri", "http://c\\", "backslash"),
        ("not a key", "x", "not a property name"),
    ],
)
async def test_a_value_that_would_change_the_file_trino_reads_is_refused(
    client: AsyncClient, key: str, value: str, problem: str
) -> None:
    refused = await _put(client, {LISTENER_FILE: {**HTTP, key: value}})

    assert refused.status_code == 422
    assert problem in refused.text


async def test_the_pointer_is_wiring_and_cannot_be_set_by_hand(client: AsyncClient) -> None:
    """Apchi writes it when there is a listener and only then. Set by hand, it would name a
    file the Secret does not hold, or keep naming one after the listener was removed."""
    refused = await _put(
        client, {"config.properties": {LISTENER_FILES_KEY: "/etc/trino/elsewhere.properties"}}
    )

    assert refused.status_code == 422
    assert f"{LISTENER_FILES_KEY} is wiring" in refused.text


async def test_the_operator_api_for_listeners_is_gone(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/event-listeners")).status_code == 404


# --- applying it -----------------------------------------------------------------------------


async def test_an_admin_apply_delivers_it_and_restarts_only_the_coordinator(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Into the Secret the chart already mounts: no mount added, so nothing on the
    Deployment for Argo CD to see but the restart."""
    mounts_before = dict(fake_kubernetes.mounts)
    await _put(applying_client, {LISTENER_FILE: HTTP})

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    secret = fake_kubernetes.secrets[SERVER_CONFIG_SECRET]
    assert secret[LISTENER_FILE].startswith("event-listener.name=http\n")
    assert LINE in secret["config.properties"]
    assert fake_kubernetes.mounts == mounts_before
    assert len(fake_kubernetes.restarts) == 1
    assert "trino-coordinator" in fake_kubernetes.restarted


async def test_the_probe_is_given_the_listener_where_the_cluster_reads_it(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """A listener Trino will not load is a coordinator that will not start (measured), so it
    has to be in front of the probe, pointed at the same way."""
    await _put(applying_client, {LISTENER_FILE: HTTP})

    await _apply(applying_client)

    assert fake_kubernetes.pod_history, "no validation probe was started"
    probe = fake_kubernetes.secret_history[fake_kubernetes.pod_history[-1]]
    assert probe[LISTENER_FILE].startswith("event-listener.name=http\n")
    assert LINE in probe["config.properties"]


async def test_removing_it_takes_both_away_and_restarts(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Loaded once per process, so the pod that has it keeps it until it restarts."""
    await _put(applying_client, {LISTENER_FILE: HTTP})
    await _apply(applying_client)
    fake_kubernetes.restarts.clear()

    await _put(applying_client, {})
    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    secret = fake_kubernetes.secrets[SERVER_CONFIG_SECRET]
    assert LISTENER_FILE not in secret
    assert LISTENER_FILES_KEY not in secret["config.properties"]
    assert len(fake_kubernetes.restarts) == 1


# --- the chart's mount -----------------------------------------------------------------------


async def test_a_coordinator_without_the_listener_directory_is_refused(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """Without it a listener would be named in config.properties and missing on disk, and the
    coordinator would not start -- found out on the Cluster rather than before."""
    spec = healthy_pod_spec()
    container = spec["containers"][0]
    container["volumeMounts"] = [
        m for m in container["volumeMounts"] if m["mountPath"] != LISTENER_DIR
    ]

    with pytest.raises(PreconditionFailed) as raised:
        await check(fake_kubernetes, pod_spec(spec), settings)

    assert f"whole at {LISTENER_DIR}" in str(raised.value)


async def test_a_subpath_mount_there_is_not_the_directory(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    """A subPath of one key is frozen to the keys that existed at pod creation, and is not
    the directory a listener added later would appear in."""
    spec = healthy_pod_spec()
    for mount in spec["containers"][0]["volumeMounts"]:
        if mount["mountPath"] == LISTENER_DIR:
            mount["subPath"] = LISTENER_FILE

    with pytest.raises(PreconditionFailed):
        await check(fake_kubernetes, pod_spec(spec), settings)

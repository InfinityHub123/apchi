"""The workers' own files, and the restart that drains rather than ends queries (ADR-0010, stage 2).

What a worker-only change must do differs from a coordinator change in exactly one way that
costs anything: it restarts the workers and leaves the coordinator alone. A catalog or a
permission never reaches a worker's files (§7.4), and the coordinator cannot drain, so a
coordinator restart for a worker change would end every query for nothing.

Proved here against the fake: the contract for worker properties, the rendered files and the
drain rule, which Deployment each change restarts, Review's account of it, the probe, the
rollback and the preconditions. That a worker really drains is tier 2's.
"""

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient

from app.config import Settings
from app.pipeline.applies import TERMINAL
from app.pipeline.impact import RESTART_WARNING, WORKER_RESTART_WARNING
from app.sections.admin import AdminValues
from app.sections.server_configuration import (
    ACCESS_CONTROL_PATH,
    CONFIG_PROPERTIES,
    JVM_CONFIG,
    LOG_PROPERTIES,
    NODE_PROPERTIES,
    WORKER_RULES_PATH,
    WORKER_WIRING,
    ServerConfiguration,
    render_worker,
    role_secret,
    worker_rules,
)
from tests.conftest import SERVER_CONFIG_SECRET, WORKER_CONFIG_SECRET, FakeKubernetes

SERVER_CONFIGURATION = "/api/v1/admin/server-configuration"
ADMIN_APPLIES = "/api/v1/admin/applies"
COORDINATOR = "trino-coordinator"
WORKER = "trino-worker"


async def _put(http: AsyncClient, **roles: dict[str, dict[str, str]]) -> Any:
    response = await http.put(SERVER_CONFIGURATION, json=roles)
    return response


async def _apply(http: AsyncClient, path: str = ADMIN_APPLIES, timeout: float = 180.0) -> dict:
    started = await http.post(path, json={})
    assert started.status_code == 202, started.text
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = (await http.get(f"/api/v1/applies/{started.json()['id']}")).json()
        if record["stage"] in TERMINAL:
            return dict(record)
        await asyncio.sleep(0.05)
    raise AssertionError("the Apply never settled")


def _plain() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


# --- the contract -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("file", "key"), [(file, key) for file, keys in WORKER_WIRING.items() for key in keys]
)
async def test_worker_wiring_is_refused_and_names_what_owns_it(
    applying_client: AsyncClient, file: str, key: str
) -> None:
    response = await _put(applying_client, worker={file: {key: "x"}})

    assert response.status_code == 422, response.text
    assert f"{key} is wiring" in response.text
    assert WORKER_WIRING[file][key] in response.text


@pytest.mark.parametrize("key", ["management.user", "management.user.https-enabled"])
async def test_a_fixed_management_user_is_refused_on_the_workers(
    applying_client: AsyncClient, key: str
) -> None:
    """It replaces the identity of the drain Apchi sends a worker, which Trino then refuses
    (measured on 483: 403), so every worker replacement would end its running tasks."""
    response = await _put(applying_client, worker={"config.properties": {key: "metrics"}})

    assert response.status_code == 422, response.text
    assert f"{key} cannot be set on the workers" in response.text
    assert "drain" in response.text


async def test_a_fixed_management_user_is_the_coordinators_to_set(
    applying_client: AsyncClient,
) -> None:
    """The coordinator is not drained, so nothing of Apchi's depends on its management
    identity."""
    response = await _put(
        applying_client, coordinator={"config.properties": {"management.user": "metrics"}}
    )

    assert response.status_code == 200, response.text


async def test_the_shutdown_grace_period_is_the_charts_because_it_must_fit_the_pods(
    applying_client: AsyncClient,
) -> None:
    """A drain longer than the termination grace period Kubernetes gives the pod is a worker
    killed mid-task, and that period is the chart's."""
    response = await _put(
        applying_client, worker={"config.properties": {"shutdown.grace-period": "1h"}}
    )

    assert response.status_code == 422, response.text
    assert "trino.worker.gracefulShutdown.gracePeriodSeconds" in response.text


async def test_worker_properties_are_stored_apart_from_the_coordinators(
    applying_client: AsyncClient,
) -> None:
    worker = {
        "config.properties": {"task.concurrency": "16"},
        "log.properties": {"io.trino": "WARN"},
    }
    assert (await _put(applying_client, worker=worker)).status_code == 200

    stored = (await applying_client.get(SERVER_CONFIGURATION)).json()

    assert stored["worker"]["config.properties"] == {"task.concurrency": "16"}
    assert stored["worker"]["log.properties"] == {"io.trino": "WARN"}
    assert stored["coordinator"]["config.properties"] == {}


async def test_a_line_break_is_refused_in_a_workers_value_too(applying_client: AsyncClient) -> None:
    response = await _put(
        applying_client, worker={"config.properties": {"task.concurrency": "8\ncoordinator=true"}}
    )

    assert response.status_code == 422, response.text
    assert "line break" in response.text


# --- the rendering ----------------------------------------------------------------------------


def test_a_workers_files_are_wiring_then_the_admins_keys() -> None:
    configuration = ServerConfiguration.model_validate(
        {"worker": {"config.properties": {"task.concurrency": "16", "a.first": "1"}}}
    )

    files = render_worker(configuration, _plain())

    assert files[CONFIG_PROPERTIES] == (
        "coordinator=false\n"
        "http-server.http.port=8080\n"
        "discovery.uri=http://trino:8080\n"
        "catalog.management=dynamic\n"
        "query.max-memory-per-node=${ENV:TRINO_QUERY_MAX_MEMORY_PER_NODE}\n"
        "memory.heap-headroom-per-node=${ENV:TRINO_MEMORY_HEAP_HEADROOM_PER_NODE}\n"
        "shutdown.grace-period=120s\n"
        "internal-communication.shared-secret=${ENV:TRINO_INTERNAL_SHARED_SECRET}\n"
        "a.first=1\n"
        "task.concurrency=16\n"
    )
    assert files[NODE_PROPERTIES] == "node.environment=production\nnode.data-dir=/data/trino\n"
    assert files[LOG_PROPERTIES] == ""
    assert files[JVM_CONFIG] == "-Djdk.attach.allowAttachSelf=true\n"
    assert files[ACCESS_CONTROL_PATH] == (
        f"access-control.name=file\nsecurity.config-file={WORKER_RULES_PATH}\n"
    )
    assert "node-scheduler.include-coordinator" not in files[CONFIG_PROPERTIES], (
        "the coordinator's default is the coordinator's"
    )


def test_the_drain_is_apchis_alone_and_metrics_are_anyones() -> None:
    """Measured on 483: a worker with no rules refuses the drain request for every identity;
    with these it accepts Apchi's and refuses everyone else's, and runs queries as before.
    Read is what `/metrics` checks, and a worker is not exposed outside the cluster (#285)."""
    import json

    rules = json.loads(worker_rules(_plain()))

    assert rules == {
        "system_information": [
            {"user": "^apchi$", "allow": ["read", "write"]},
            {"allow": ["read"]},
        ]
    }


def test_the_drain_rule_matches_the_identity_exactly_and_only_it() -> None:
    """The user is a regular expression in Trino's rules, so an identity with a dot in it
    would otherwise also match one with any character there."""
    import json
    import re

    settings = _plain().model_copy(update={"trino_user": "svc.apchi"})
    pattern = json.loads(worker_rules(settings))["system_information"][0]["user"]

    assert re.fullmatch(pattern, "svc.apchi")
    assert not re.fullmatch(pattern, "svcXapchi")
    assert not re.fullmatch(pattern, "svc.apchi2")


def test_the_grace_period_comes_from_the_setting_the_chart_passes() -> None:
    settings = _plain().model_copy(update={"trino_worker_shutdown_grace_period": "5s"})

    rendered = render_worker(ServerConfiguration(), settings)[CONFIG_PROPERTIES]

    assert "shutdown.grace-period=5s\n" in rendered


# --- which Deployment restarts -----------------------------------------------------------------


async def test_a_worker_only_change_restarts_the_workers_and_not_the_coordinator(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings: Settings
) -> None:
    await _put(applying_client, worker={"config.properties": {"task.concurrency": "16"}})

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    assert fake_kubernetes.restarted == [WORKER]
    assert "worker server configuration" in fake_kubernetes.restarts[0]
    expected = role_secret(
        "worker",
        AdminValues.model_validate(
            {"server_configuration": {"worker": {"config.properties": {"task.concurrency": "16"}}}}
        ).server_configuration,
        settings,
    )
    assert fake_kubernetes.secrets[WORKER_CONFIG_SECRET] == expected


async def test_a_coordinator_only_change_leaves_the_workers_alone(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await _put(applying_client, coordinator={"config.properties": {"query.max-run-time": "4h"}})

    await _apply(applying_client)

    assert fake_kubernetes.restarted == [COORDINATOR]


async def test_both_changing_restart_both_in_one_rollout(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Together: the coordinator's restart ends every query anyway, so making it wait behind
    a worker drain would only lengthen that outage."""
    await _put(
        applying_client,
        coordinator={"config.properties": {"query.max-run-time": "4h"}},
        worker={"config.properties": {"task.concurrency": "16"}},
    )

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    assert fake_kubernetes.restarted == [COORDINATOR, WORKER]
    assert [e["stage"] for e in record["history"]].count("rolling_out") == 1


async def test_a_raw_file_restarts_only_the_coordinator_it_is_mounted_on(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    response = await applying_client.put(
        "/api/v1/admin/files/etc/trino/session-property-config.properties",
        json={"content": "session-property-config.configuration-manager=file\n"},
    )
    assert response.status_code == 200, response.text

    await _apply(applying_client)

    assert fake_kubernetes.restarted == [COORDINATOR]


async def test_nothing_changed_restarts_nothing(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await _put(applying_client, worker={"config.properties": {"task.concurrency": "16"}})
    await _apply(applying_client)

    await _apply(applying_client)
    await _apply(applying_client, "/api/v1/applies")

    assert fake_kubernetes.restarted == [WORKER]


async def test_a_worker_secret_edited_by_hand_is_put_back_with_a_worker_restart(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    held = dict(fake_kubernetes.secrets[WORKER_CONFIG_SECRET])
    fake_kubernetes.secrets[WORKER_CONFIG_SECRET] = {**held, "log.properties": "io.trino=DEBUG\n"}

    await _apply(applying_client, "/api/v1/applies")

    assert fake_kubernetes.secrets[WORKER_CONFIG_SECRET] == held
    assert fake_kubernetes.restarted == [WORKER]


async def test_an_upgraded_cluster_gets_the_metrics_rule_at_its_next_apply(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Apchi never rewrites a Secret at startup, so a Cluster upgraded from 0.4 keeps the
    rules that refused every scrape until an Apply delivers these (#285)."""
    held = dict(fake_kubernetes.secrets[WORKER_CONFIG_SECRET])
    fake_kubernetes.secrets[WORKER_CONFIG_SECRET] = {
        **held,
        WORKER_RULES_PATH: '{"system_information": [{"user": "^apchi$", "allow": ["write"]}]}\n',
    }

    review = (await applying_client.get("/api/v1/review")).json()
    await _apply(applying_client, "/api/v1/applies")

    assert review["cost"]["restarts_workers"] is True
    assert review["cost"]["restarts_coordinator"] is False
    assert fake_kubernetes.secrets[WORKER_CONFIG_SECRET] == held
    assert fake_kubernetes.restarted == [WORKER]


# --- Review -----------------------------------------------------------------------------------


async def test_review_says_a_worker_change_restarts_the_workers_and_costs_no_query(
    applying_client: AsyncClient,
) -> None:
    await _put(applying_client, worker={"config.properties": {"task.concurrency": "16"}})

    cost = (await applying_client.get("/api/v1/review")).json()["cost"]

    assert cost["restarts_workers"] is True
    assert cost["restarts_coordinator"] is False
    assert cost["queries_at_risk"] is None
    assert cost["warning"] == WORKER_RESTART_WARNING


async def test_review_leads_with_the_coordinator_when_both_restart(
    applying_client: AsyncClient,
) -> None:
    """The coordinator's restart is the one that costs queries, so its warning is the one shown."""
    await _put(
        applying_client,
        coordinator={"config.properties": {"query.max-run-time": "4h"}},
        worker={"config.properties": {"task.concurrency": "16"}},
    )

    cost = (await applying_client.get("/api/v1/review")).json()["cost"]

    assert cost["restarts_coordinator"] is True
    assert cost["restarts_workers"] is True
    assert cost["warning"] == RESTART_WARNING


async def test_review_after_the_apply_says_nothing_restarts(
    applying_client: AsyncClient,
) -> None:
    await _put(applying_client, worker={"config.properties": {"task.concurrency": "16"}})
    await _apply(applying_client)

    cost = (await applying_client.get("/api/v1/review")).json()["cost"]

    assert cost["restarts_workers"] is False
    assert cost["warning"] is None


# --- the probe --------------------------------------------------------------------------------


async def test_worker_properties_are_proved_on_the_probe(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Measured on 483: a coordinator accepts worker-side properties and refuses a misspelled
    one, so one probe judges both roles."""
    await _put(
        applying_client,
        coordinator={"config.properties": {"query.max-run-time": "4h"}},
        worker={
            "config.properties": {"task.concurrency": "16"},
            "log.properties": {"io.trino.execution": "DEBUG"},
        },
    )

    await _apply(applying_client)

    assert len(fake_kubernetes.pod_history) == 1, "no clash, so one probe"
    probe = fake_kubernetes.secret_history[fake_kubernetes.pod_history[0]]
    assert "task.concurrency=16\n" in probe["config.properties"]
    assert "query.max-run-time=4h\n" in probe["config.properties"]
    assert "io.trino.execution=DEBUG\n" in probe["log.properties"]
    assert "shutdown.grace-period" not in probe["config.properties"], "wiring is the probe's"


async def test_a_key_the_roles_disagree_on_is_proved_on_a_probe_of_the_workers_own(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """One probe holds one value of a key. Both are on trial, so the workers' gets its own."""
    await _put(
        applying_client,
        coordinator={"config.properties": {"task.concurrency": "4"}},
        worker={"config.properties": {"task.concurrency": "16"}},
    )

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    assert len(fake_kubernetes.pod_history) == 2
    workers, coordinator = (fake_kubernetes.secret_history[p] for p in fake_kubernetes.pod_history)
    assert fake_kubernetes.pod_history[0].endswith("-workers")
    assert "task.concurrency=16\n" in workers["config.properties"]
    assert "task.concurrency=4\n" in coordinator["config.properties"]


async def test_a_refusal_on_the_workers_probe_says_whose_properties_they_were(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await _put(
        applying_client,
        coordinator={"config.properties": {"task.concurrency": "4"}},
        worker={"config.properties": {"task.concurrency": "16"}},
    )
    fake_kubernetes.pod_problem = "CrashLoopBackOff"

    record = await _apply(applying_client)

    assert record["stage"] == "failed", record
    assert "The workers' properties" in record["failure_reason"]
    assert fake_kubernetes.restarted == []


# --- rollback ---------------------------------------------------------------------------------


async def test_a_failed_worker_change_is_put_back_and_the_workers_restarted_onto_it(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await _put(applying_client, worker={"config.properties": {"task.concurrency": "16"}})
    await _apply(applying_client)
    good = dict(fake_kubernetes.secrets[WORKER_CONFIG_SECRET])
    await _put(applying_client, worker={"config.properties": {"task.concurrency": "2"}})
    fake_kubernetes.report_a_missing_worker_once = True

    record = await _apply(applying_client)

    assert record["stage"] == "failed", record
    assert fake_kubernetes.secrets[WORKER_CONFIG_SECRET] == good
    assert fake_kubernetes.restarted == [WORKER, WORKER, WORKER], fake_kubernetes.restarts
    stored = (await applying_client.get(SERVER_CONFIGURATION)).json()["worker"]
    assert stored["config.properties"] == {"task.concurrency": "16"}


async def test_rolling_back_a_worker_change_leaves_the_coordinator_running(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await _put(applying_client, worker={"config.properties": {"task.concurrency": "2"}})
    fake_kubernetes.report_a_missing_worker_once = True

    await _apply(applying_client)

    assert COORDINATOR not in fake_kubernetes.restarted


# --- the preconditions ------------------------------------------------------------------------


def _worker_mounts(fake: FakeKubernetes) -> list[dict[str, Any]]:
    assert fake.worker_pod_spec is not None
    return fake.worker_pod_spec["containers"][0]["volumeMounts"]  # type: ignore[no-any-return]


async def test_a_worker_that_does_not_mount_its_rules_refuses_the_apply(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Every file Apchi renders for a role has to be mounted, or Apchi would report a change
    applied that Trino never reads -- here, the rule the drain depends on."""
    mounts = _worker_mounts(fake_kubernetes)
    mounts[:] = [m for m in mounts if m["mountPath"] != WORKER_RULES_PATH]

    record = await _apply(applying_client, "/api/v1/applies")

    assert record["stage"] == "failed", record
    assert f"The worker does not mount {WORKER_RULES_PATH}" in record["failure_reason"]


async def test_a_coordinator_file_mounted_from_somewhere_else_refuses_the_apply(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    for mount in fake_kubernetes.pod_spec["containers"][0]["volumeMounts"]:
        if mount["mountPath"] == "/etc/trino/log.properties":
            mount["name"] = "elsewhere"
    fake_kubernetes.pod_spec["volumes"].append({"name": "elsewhere", "secret": {"secretName": "x"}})

    record = await _apply(applying_client, "/api/v1/applies")

    assert record["stage"] == "failed", record
    reason = record["failure_reason"]
    assert "The coordinator does not mount /etc/trino/log.properties" in reason
    assert "The coordinator's container 'trino' mounts 'elsewhere'" in reason


async def test_a_configmap_under_etc_trino_on_a_worker_refuses_the_apply(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    _worker_mounts(fake_kubernetes).append(
        {"name": "chart", "mountPath": "/etc/trino/exchange-manager.properties", "subPath": "x"}
    )
    assert fake_kubernetes.worker_pod_spec is not None
    fake_kubernetes.worker_pod_spec["volumes"].append({"name": "chart", "configMap": {"name": "c"}})

    record = await _apply(applying_client, "/api/v1/applies")

    assert record["stage"] == "failed", record
    assert "The worker's container 'trino' mounts 'chart'" in record["failure_reason"]


async def test_a_world_readable_worker_configuration_refuses_the_apply(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    assert fake_kubernetes.worker_pod_spec is not None
    for volume in fake_kubernetes.worker_pod_spec["volumes"]:
        if volume["name"] == "server-config":
            volume["secret"]["defaultMode"] = 0o644

    record = await _apply(applying_client, "/api/v1/applies")

    assert record["stage"] == "failed", record
    assert "The worker's 'server-config' volume holds credentials" in record["failure_reason"]


# --- the first write --------------------------------------------------------------------------


async def test_both_roles_are_written_on_start_when_missing_and_never_rewritten(
    settings: Settings, fake_kubernetes: FakeKubernetes
) -> None:
    from httpx import ASGITransport

    from app.adapters.trino import Trino
    from app.main import create_app

    del fake_kubernetes.secrets[WORKER_CONFIG_SECRET]
    held = {**fake_kubernetes.secrets[SERVER_CONFIG_SECRET], "log.properties": "held\n"}
    fake_kubernetes.secrets[SERVER_CONFIG_SECRET] = held
    app = create_app(settings)
    app.state.kubernetes = fake_kubernetes
    app.state.trino = Trino(url="http://127.0.0.1:1")
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="https://apchi"),
        app.router.lifespan_context(app),
    ):
        for _ in range(100):
            if WORKER_CONFIG_SECRET in fake_kubernetes.secrets:
                break
            await asyncio.sleep(0.02)

    assert fake_kubernetes.secrets[WORKER_CONFIG_SECRET] == role_secret(
        "worker", AdminValues().server_configuration, settings
    )
    assert fake_kubernetes.secrets[SERVER_CONFIG_SECRET] == held

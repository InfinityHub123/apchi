"""Raw Files and the Server Configuration in one pipeline (§14, ADR-0010).

The two are separate surfaces for one directory, so what is worth proving is where they meet:

* a Raw File stored at a path Apchi now renders -- one written before this stage -- stops every
  Apply, naming itself, rather than being delivered over Apchi's file
* both change in one Admin Apply, and the coordinator restarts once for the pair
* a Raw File alone is now validated on the probe, which it was not when nothing else was
* a failed Admin Apply puts both back, and restarts the coordinator for it
"""

import asyncio

from httpx import AsyncClient

from app.pipeline.applies import TERMINAL
from app.pipeline.raw_files import secret_key
from tests.conftest import SERVER_CONFIG_SECRET, FakeKubernetes

SERVER_CONFIGURATION = "/api/v1/admin/server-configuration"
ADMIN_APPLIES = "/api/v1/admin/applies"
FILES = "/api/v1/admin/files"
SESSION_FILE = "/etc/trino/session-property-config.properties"
SESSION_CONTENT = (
    "session-property-config.configuration-manager=file\n"
    "session-property-manager.config-file=/etc/trino/session-properties.json\n"
)


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


async def _raw(http: AsyncClient, path: str, content: str) -> None:
    response = await http.put(f"{FILES}{path}", json={"content": content})
    assert response.status_code == 200, response.text


async def _properties(http: AsyncClient, config: dict[str, str]) -> None:
    response = await http.put(
        SERVER_CONFIGURATION, json={"coordinator": {"config.properties": config}}
    )
    assert response.status_code == 200, response.text


def _app_state(http: AsyncClient) -> object:
    return http._transport.app.state  # type: ignore[attr-defined]


async def test_a_raw_file_stranded_at_a_rendered_path_stops_every_apply(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """Written before Apchi rendered the file, so it is in MongoDB already: the write path
    refuses it now, and the Apply must too. Delivering it would replace Apchi's config.properties
    whole, wiring and all."""
    state = _app_state(applying_client)
    values = await state.admin_store.load()  # type: ignore[attr-defined]
    await state.admin_store.save(  # type: ignore[attr-defined]
        values.model_copy(update={"raw_files": {"/etc/trino/config.properties": "x=y\n"}})
    )

    for path in ("/api/v1/applies", ADMIN_APPLIES):
        record = await _apply(applying_client, path)

        assert record["stage"] == "failed", record
        reason = record["failure_reason"]
        assert "/etc/trino/config.properties" in reason
        assert "/api/v1/admin/server-configuration" in reason
        assert "DELETE /api/v1/admin/files/etc/trino/config.properties" in reason
    assert fake_kubernetes.restarts == []
    assert "/etc/trino/config.properties" not in fake_kubernetes.mounts


async def test_removing_the_stranded_file_lets_the_next_apply_through(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    state = _app_state(applying_client)
    values = await state.admin_store.load()  # type: ignore[attr-defined]
    await state.admin_store.save(  # type: ignore[attr-defined]
        values.model_copy(update={"raw_files": {"/etc/trino/log.properties": "io.trino=DEBUG\n"}})
    )
    assert (await applying_client.delete(f"{FILES}/etc/trino/log.properties")).status_code == 204

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record


async def test_both_in_one_admin_apply_restart_the_coordinator_once(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings: object
) -> None:
    await _raw(applying_client, SESSION_FILE, SESSION_CONTENT)
    await _properties(applying_client, {"query.max-execution-time": "3h"})

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    assert len(fake_kubernetes.restarts) == 1, fake_kubernetes.restarts
    assert "server configuration" in fake_kubernetes.restarts[0]
    assert "raw files" in fake_kubernetes.restarts[0]
    assert (
        "query.max-execution-time=3h"
        in (fake_kubernetes.secrets[SERVER_CONFIG_SECRET]["config.properties"])
    )
    raw_secret = fake_kubernetes.secrets[settings.raw_files_secret_name]  # type: ignore[attr-defined]
    assert raw_secret == {secret_key(SESSION_FILE): SESSION_CONTENT}
    assert SESSION_FILE in fake_kubernetes.mounts


async def test_the_probe_holds_both(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await _raw(applying_client, SESSION_FILE, SESSION_CONTENT)
    await _properties(applying_client, {"query.max-execution-time": "3h"})

    await _apply(applying_client)

    probe = fake_kubernetes.secret_history[fake_kubernetes.pod_history[-1]]
    assert probe["session-property-config.properties"] == SESSION_CONTENT
    assert "query.max-execution-time=3h\n" in probe["config.properties"]


async def test_a_raw_file_alone_is_validated_on_the_probe(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    """With nothing in any Section there was nothing else to probe, and the raw file went to
    the coordinator unvalidated -- the class §14 calls most able to stop Trino booting."""
    await _raw(applying_client, SESSION_FILE, SESSION_CONTENT)

    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    assert fake_kubernetes.pod_history, "no probe was started for a raw file"
    probe = fake_kubernetes.secret_history[fake_kubernetes.pod_history[-1]]
    assert probe["session-property-config.properties"] == SESSION_CONTENT


async def test_a_raw_file_may_refer_to_the_sizing_the_chart_sets(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings: object
) -> None:
    """Delivered verbatim: Trino substitutes ${ENV:...} itself, in any file it reads."""
    content = "exchange.max-buffer-size=${ENV:TRINO_QUERY_MAX_MEMORY_PER_NODE}\n"
    await _raw(applying_client, "/etc/trino/exchange-manager.properties", content)

    await _apply(applying_client)

    raw_secret = fake_kubernetes.secrets[settings.raw_files_secret_name]  # type: ignore[attr-defined]
    assert raw_secret[secret_key("/etc/trino/exchange-manager.properties")] == content


async def test_removing_a_raw_file_leaves_the_server_configuration_alone(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes
) -> None:
    await _raw(applying_client, SESSION_FILE, SESSION_CONTENT)
    await _properties(applying_client, {"query.max-execution-time": "3h"})
    await _apply(applying_client)
    rendered = dict(fake_kubernetes.secrets[SERVER_CONFIG_SECRET])

    assert (await applying_client.delete(f"{FILES}{SESSION_FILE}")).status_code == 204
    record = await _apply(applying_client)

    assert record["stage"] == "succeeded", record
    assert SESSION_FILE not in fake_kubernetes.mounts
    assert fake_kubernetes.secrets[SERVER_CONFIG_SECRET] == rendered
    assert len(fake_kubernetes.restarts) == 2, "removing a file Trino read at start restarts it"


async def test_a_failed_admin_apply_puts_both_back_and_restarts_for_it(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings: object
) -> None:
    """Before this, an Admin Apply's rollback rewrote the raw files *after* it had restarted
    and verified, so a restored file reached the coordinator only if some Section happened to
    restart it."""
    await _raw(applying_client, SESSION_FILE, SESSION_CONTENT)
    await _properties(applying_client, {"query.max-execution-time": "3h"})
    await _apply(applying_client)
    good_raw = dict(fake_kubernetes.secrets[settings.raw_files_secret_name])  # type: ignore[attr-defined]
    good_config = dict(fake_kubernetes.secrets[SERVER_CONFIG_SECRET])

    await _raw(applying_client, SESSION_FILE, "session-property-config.configuration-manager=x\n")
    await _properties(applying_client, {"query.max-execution-time": "1h"})
    fake_kubernetes.report_a_missing_worker_once = True
    record = await _apply(applying_client)

    assert record["stage"] == "failed", record
    assert fake_kubernetes.secrets[settings.raw_files_secret_name] == good_raw  # type: ignore[attr-defined]
    assert fake_kubernetes.secrets[SERVER_CONFIG_SECRET] == good_config
    assert len(fake_kubernetes.restarts) == 3, fake_kubernetes.restarts
    assert "auto rollback" in fake_kubernetes.restarts[-1]


async def test_a_failed_raw_file_change_alone_is_restarted_back_too(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings: object
) -> None:
    """The bug this stage fixed, on its own: only a raw file moved, so no Section asked for
    the rollback's restart."""
    await _raw(applying_client, SESSION_FILE, SESSION_CONTENT)
    await _apply(applying_client)
    good = dict(fake_kubernetes.secrets[settings.raw_files_secret_name])  # type: ignore[attr-defined]

    await _raw(applying_client, SESSION_FILE, "session-property-config.configuration-manager=x\n")
    fake_kubernetes.report_a_missing_worker_once = True
    record = await _apply(applying_client)

    assert record["stage"] == "failed", record
    assert fake_kubernetes.secrets[settings.raw_files_secret_name] == good  # type: ignore[attr-defined]
    assert len(fake_kubernetes.restarts) == 3, fake_kubernetes.restarts


async def test_an_operator_apply_records_what_the_cluster_has_too(
    applying_client: AsyncClient, fake_kubernetes: FakeKubernetes, settings: object
) -> None:
    """Every Apply delivers the Admin values in force, so an Operator's is a delivery too: a
    later Admin Apply that fails goes back to what it delivered, not further."""
    await _raw(applying_client, SESSION_FILE, SESSION_CONTENT)
    assert (await _apply(applying_client, "/api/v1/applies"))["stage"] == "succeeded"

    await _raw(applying_client, SESSION_FILE, "session-property-config.configuration-manager=x\n")
    await _properties(applying_client, {"query.max-execution-time": "1h"})
    fake_kubernetes.report_a_missing_worker_once = True
    record = await _apply(applying_client)

    assert record["stage"] == "failed", record
    stored = (await applying_client.get(SERVER_CONFIGURATION)).json()["coordinator"]
    assert stored["config.properties"] == {}
    raw_secret = fake_kubernetes.secrets[settings.raw_files_secret_name]  # type: ignore[attr-defined]
    assert raw_secret == {secret_key(SESSION_FILE): SESSION_CONTENT}


async def test_before_any_apply_has_recorded_one_a_rollback_goes_one_write_back(
    applying_client: AsyncClient,
) -> None:
    """An install upgrading from before the record existed has no copy of what it delivered.
    Going back to the defaults there would wipe its Admin values on the first failed Apply,
    so it goes back one write, as it always did."""
    state = _app_state(applying_client)
    await _properties(applying_client, {"query.max-execution-time": "3h"})
    await _properties(applying_client, {"query.max-execution-time": "1h"})

    restored = await state.admin_store.restore_previous()  # type: ignore[attr-defined]

    assert restored.server_configuration.coordinator.config_properties == {
        "query.max-execution-time": "3h"
    }

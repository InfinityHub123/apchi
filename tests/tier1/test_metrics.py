"""Prometheus metrics (#280): what a monitoring system is told, and that it needs no identity."""

import asyncio
import math
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from prometheus_client.parser import text_string_to_metric_families

from app.adapters.trino import Trino
from app.config import Settings
from app.main import create_app
from app.metrics import Metrics
from app.pipeline.applies import TERMINAL, ApplyRecord, Rollback, Stage, StageEvent
from tests.conftest import FakeKubernetes

CATALOG = {"name": "metered", "connector": "memory", "properties": {}}
T0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def _record(
    *stages: tuple[Stage, float],
    kind: str = "apply",
    rollback: Rollback | None = None,
    interrupted: bool = False,
) -> ApplyRecord:
    """An Apply whose history enters each stage the given number of seconds after T0."""
    history = [StageEvent(stage=stage, at=T0 + timedelta(seconds=at)) for stage, at in stages]
    return ApplyRecord(
        id="apl_test",
        kind=kind,  # type: ignore[arg-type]
        stage=history[-1].stage,
        history=history,
        rollback=rollback,
        interrupted=interrupted,
        started_at=T0,
        finished_at=history[-1].at if history[-1].stage in TERMINAL else None,
    )


def _value(metrics: Metrics, name: str, **labels: str) -> float | None:
    return metrics.registry.get_sample_value(name, labels)


def _samples(body: str) -> dict[tuple[str, frozenset[tuple[str, str]]], float]:
    return {
        (sample.name, frozenset(sample.labels.items())): sample.value
        for family in text_string_to_metric_families(body)
        for sample in family.samples
    }


# --- what one finished Apply counts as ---------------------------------------------------------


def test_a_succeeded_apply_counts_once_with_its_duration_and_each_stage() -> None:
    metrics = Metrics()
    metrics.observe(
        _record(
            (Stage.VALIDATING, 0),
            (Stage.APPLYING, 10),
            (Stage.ROLLING_OUT, 12),
            (Stage.VERIFYING, 252),
            (Stage.COMMITTING, 262),
            (Stage.SUCCEEDED, 263),
        )
    )

    assert _value(metrics, "apchi_applies_total", kind="apply", outcome="succeeded") == 1
    assert _value(
        metrics, "apchi_apply_duration_seconds_sum", kind="apply", outcome="succeeded"
    ) == pytest.approx(263)
    assert _value(
        metrics, "apchi_apply_stage_duration_seconds_sum", kind="apply", stage="rolling_out"
    ) == pytest.approx(240)
    assert (
        _value(metrics, "apchi_apply_stage_duration_seconds_count", kind="apply", stage="succeeded")
        is None
    ), "the terminal stage is never left, so it has no duration"


def test_an_auto_rollback_is_counted_with_its_outcome() -> None:
    metrics = Metrics()
    metrics.observe(
        _record(
            (Stage.VALIDATING, 0),
            (Stage.APPLYING, 1),
            (Stage.VERIFYING, 2),
            (Stage.ROLLING_BACK, 5),
            (Stage.INCIDENT, 9),
            kind="admin_apply",
            rollback=Rollback.FAILED,
        )
    )

    assert _value(metrics, "apchi_applies_total", kind="admin_apply", outcome="incident") == 1
    assert _value(metrics, "apchi_auto_rollbacks_total", kind="admin_apply", outcome="failed") == 1
    assert _value(
        metrics, "apchi_apply_stage_duration_seconds_sum", kind="admin_apply", stage="rolling_back"
    ) == pytest.approx(4)


def test_an_interrupted_apply_is_counted_without_durations() -> None:
    """Its durations would include however long Apchi was down."""
    metrics = Metrics()
    metrics.observe(
        _record(
            (Stage.VALIDATING, 0),
            (Stage.APPLYING, 1),
            (Stage.FAILED, 3600),
            rollback=Rollback.SUCCEEDED,
            interrupted=True,
        )
    )

    assert _value(metrics, "apchi_applies_total", kind="apply", outcome="failed") == 1
    assert _value(metrics, "apchi_applies_interrupted_total", kind="apply") == 1
    assert (
        _value(metrics, "apchi_apply_duration_seconds_count", kind="apply", outcome="failed")
        is None
    )
    assert (
        _value(metrics, "apchi_apply_stage_duration_seconds_count", kind="apply", stage="applying")
        is None
    )


def test_an_apply_still_in_flight_is_not_counted() -> None:
    metrics = Metrics()
    metrics.observe(_record((Stage.VALIDATING, 0), (Stage.APPLYING, 1)))

    assert _value(metrics, "apchi_applies_total", kind="apply", outcome="applying") is None


def test_a_state_that_could_not_be_read_is_nan_not_zero() -> None:
    """Zero would say Maintenance Mode is off while MongoDB is down and nobody knows."""
    metrics = Metrics()
    samples = _samples(metrics.scrape(maintenance=None, in_flight=None).decode())

    assert math.isnan(samples[("apchi_maintenance_mode", frozenset())])
    assert math.isnan(samples[("apchi_apply_in_flight", frozenset())])


def test_each_loaded_certificate_reports_its_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    expires = T0 + timedelta(days=30)
    monkeypatch.setattr("app.trust.expiries", lambda: {"/etc/apchi/tls/trino/tls.crt": expires})
    samples = _samples(Metrics().scrape(maintenance=False, in_flight=False).decode())

    assert (
        samples[
            (
                "apchi_certificate_expiry_timestamp_seconds",
                frozenset({("path", "/etc/apchi/tls/trino/tls.crt")}),
            )
        ]
        == expires.timestamp()
    )


def test_two_apps_do_not_count_each_others_applies() -> None:
    """Tier 1 builds an app per test, and a real process builds one; either way an Apply is
    counted by the app that ran it."""
    first, second = Metrics(), Metrics()
    first.observe(_record((Stage.VALIDATING, 0), (Stage.SUCCEEDED, 1)))

    assert _value(second, "apchi_applies_total", kind="apply", outcome="succeeded") is None


# --- through the HTTP API ----------------------------------------------------------------------


async def _scrape(client: AsyncClient) -> dict[tuple[str, frozenset[tuple[str, str]]], float]:
    response = await client.get("/metrics")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    return _samples(response.text)


async def test_an_apply_through_the_api_is_counted(applying_client: AsyncClient) -> None:
    created = await applying_client.post("/api/v1/catalogs", json=CATALOG)
    assert created.status_code == 201, created.text
    started = await applying_client.post("/api/v1/applies")
    assert started.status_code == 202, started.text

    succeeded = ("apchi_applies_total", frozenset({("kind", "apply"), ("outcome", "succeeded")}))
    deadline = asyncio.get_running_loop().time() + 120
    samples = await _scrape(applying_client)
    while succeeded not in samples:
        assert asyncio.get_running_loop().time() < deadline, "the Apply was never counted"
        await asyncio.sleep(0.1)
        samples = await _scrape(applying_client)

    assert samples[succeeded] == 1
    assert samples[("apchi_apply_in_flight", frozenset())] == 0
    assert (
        samples[
            (
                "apchi_apply_stage_duration_seconds_count",
                frozenset({("kind", "apply"), ("stage", "validating")}),
            )
        ]
        == 1
    )


async def test_maintenance_mode_is_reported_while_engaged(client: AsyncClient) -> None:
    assert (await _scrape(client))[("apchi_maintenance_mode", frozenset())] == 0

    engaged = await client.put(
        "/api/v1/admin/maintenance-mode", json={"engaged": True, "reason": "upgrading Trino"}
    )
    assert engaged.status_code == 200, engaged.text
    assert (await _scrape(client))[("apchi_maintenance_mode", frozenset())] == 1

    released = await client.put("/api/v1/admin/maintenance-mode", json={"engaged": False})
    assert released.status_code == 200, released.text
    assert (await _scrape(client))[("apchi_maintenance_mode", frozenset())] == 0


async def test_the_version_is_reported(client: AsyncClient) -> None:
    from app.version import VERSION

    samples = await _scrape(client)

    assert samples[("apchi_info", frozenset({("version", VERSION)}))] == 1


async def test_metrics_can_be_turned_off(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"metrics_enabled": False}))
    app.state.kubernetes = FakeKubernetes()
    app.state.trino = Trino(url="http://127.0.0.1:1")
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="https://apchi") as client,
        app.router.lifespan_context(app),
    ):
        assert (await client.get("/metrics")).status_code == 404

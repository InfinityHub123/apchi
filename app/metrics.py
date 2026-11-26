"""What Apchi tells a monitoring system (#280).

Two kinds of number, kept apart because they answer differently across replicas and
restarts:

- **The Cluster's state** -- Maintenance Mode, an Apply in flight -- is read from MongoDB
  at scrape time. Every replica answers the same, and a restart loses nothing.
- **What happened to Applies** is counted by the process that finished them. `sum()`
  across replicas is the total, and a restart resets the counters, which `rate()` and
  `increase()` already handle.

Every Apply is counted from its finished record, in `observe`, so the pipeline and restart
recovery cannot count the same Apply differently.
"""

from datetime import datetime

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector
from prometheus_client.registry import Collector

from app import trust
from app.pipeline.applies import TERMINAL, ApplyRecord
from app.version import VERSION

#: An Apply without a Rollout takes seconds; one with a Rollout waits for the coordinator
#: and then every worker to come back, which is minutes on a real Cluster and can run to the
#: rollout timeouts.
_APPLY_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 900, 1200, 1800)

#: The scrape's content type, for the route to answer with.
CONTENT_TYPE = CONTENT_TYPE_LATEST


class _LoadedCertificates(Collector):
    """The expiry of each certificate Apchi *loaded*, read at scrape time.

    Of what was loaded, not of what is mounted now: after a rotation the two are different
    certificates, and Apchi keeps presenting the first until it restarts (#193). This is what
    lets a certificate near expiry be alerted on without an Apply to warn about it (#196).
    """

    def collect(self) -> list[GaugeMetricFamily]:
        family = GaugeMetricFamily(
            "apchi_certificate_expiry_timestamp_seconds",
            "When a certificate Apchi loaded at startup stops being valid.",
            labels=["path"],
        )
        for path, expires in sorted(trust.expiries().items()):
            family.add_metric([path], expires.timestamp())
        return [family]


class Metrics:
    """One app's metrics, in a registry of its own."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        ProcessCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)
        self.registry.register(_LoadedCertificates())
        Info("apchi", "The running Apchi.", registry=self.registry).info({"version": VERSION})

        self._maintenance = Gauge(
            "apchi_maintenance_mode",
            "1 while Maintenance Mode is engaged, which an incident does by itself.",
            registry=self.registry,
        )
        self._in_flight = Gauge(
            "apchi_apply_in_flight",
            "1 while an Apply is running, on any replica.",
            registry=self.registry,
        )
        self._applies = Counter(
            "apchi_applies",
            "Applies this process finished, by kind and terminal stage.",
            ["kind", "outcome"],
            registry=self.registry,
        )
        self._duration = Histogram(
            "apchi_apply_duration_seconds",
            "How long an Apply took, from start to its terminal stage.",
            ["kind", "outcome"],
            buckets=_APPLY_BUCKETS,
            registry=self.registry,
        )
        self._stage_duration = Histogram(
            "apchi_apply_stage_duration_seconds",
            "How long an Apply spent in each stage it went on from.",
            ["kind", "stage"],
            buckets=_APPLY_BUCKETS,
            registry=self.registry,
        )
        self._rollbacks = Counter(
            "apchi_auto_rollbacks",
            "Auto Rollbacks this process finished, by the kind of Apply and their outcome.",
            ["kind", "outcome"],
            registry=self.registry,
        )
        self._interrupted = Counter(
            "apchi_applies_interrupted",
            "Applies a restart of Apchi ended, counted by the process that recovered them.",
            ["kind"],
            registry=self.registry,
        )

    def observe(self, record: ApplyRecord) -> None:
        """Count one finished Apply."""
        if record.stage not in TERMINAL:
            return
        self._applies.labels(record.kind, record.stage.value).inc()
        if record.rollback is not None:
            self._rollbacks.labels(record.kind, record.rollback.value).inc()
        if record.interrupted:
            # Its durations would include however long Apchi was down, which says nothing
            # about the Apply.
            self._interrupted.labels(record.kind).inc()
            return
        if record.finished_at is not None:
            self._duration.labels(record.kind, record.stage.value).observe(
                _seconds(record.started_at, record.finished_at)
            )
        for event, following in zip(record.history, record.history[1:], strict=False):
            self._stage_duration.labels(record.kind, event.stage.value).observe(
                _seconds(event.at, following.at)
            )

    def scrape(self, *, maintenance: bool | None, in_flight: bool | None) -> bytes:
        """The exposition, with the Cluster's state as the caller just read it.

        None for a state that could not be read, which exports NaN: the last value would
        read as current, and a zero would say Maintenance Mode is off when nobody knows.
        """
        self._maintenance.set(_present(maintenance))
        self._in_flight.set(_present(in_flight))
        return generate_latest(self.registry)


def _present(state: bool | None) -> float:
    return float("nan") if state is None else float(state)


def _seconds(start: datetime, end: datetime) -> float:
    return max((end - start).total_seconds(), 0.0)

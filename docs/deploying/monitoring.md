# Monitoring

Apchi serves Prometheus metrics at `/metrics`, on the same port as its API. Scraping needs no
identity, whatever `auth.mode` is: a scraper has no Apchi identity to present. That is safe
only because nothing exported names a resource, an identity or a credential. The metrics carry
counts, stages, kinds of Apply and the paths of mounted certificates.

To turn them on with the Prometheus Operator, or with OpenShift's user-workload monitoring:

```yaml
metrics:
  serviceMonitor:
    enabled: true
    # Whatever your Prometheus' serviceMonitorSelector matches.
    labels: {release: kube-prometheus-stack}
```

To serve no metrics at all, set `metrics.enabled: false`.

## What is exported

**The Cluster's state.** Read from MongoDB at each scrape, so every replica gives the same
answer and a restart loses nothing. If MongoDB can't be read, the value is `NaN`, never the
last value or a zero, so Maintenance Mode is never reported as off when nobody knows.

| Metric | |
| --- | --- |
| `apchi_maintenance_mode` | 1 while Maintenance Mode is engaged. An Admin engages it, and so does an Apply whose Auto Rollback failed |
| `apchi_apply_in_flight` | 1 while an Apply is running, on any replica |

**What happened to Applies.** These are counted by the process that finished each Apply, so use
`sum()` across replicas. A restart resets them, which `rate()` and `increase()` already handle.
`kind` is `apply` (an Operator's) or `admin_apply`.

| Metric | Labels | |
| --- | --- | --- |
| `apchi_applies_total` | `kind`, `outcome` | Finished Applies. `outcome` is the terminal stage: `succeeded`, `failed` or `incident` |
| `apchi_apply_duration_seconds` | `kind`, `outcome` | A histogram, from start to terminal stage. Buckets run to 30 minutes, since a Rollout waits for every Trino pod |
| `apchi_apply_stage_duration_seconds` | `kind`, `stage` | How long each stage took, counting only stages the Apply went on from. `rolling_out` is the restart, and `rolling_back` the Auto Rollback |
| `apchi_auto_rollbacks_total` | `kind`, `outcome` | Auto Rollbacks, `succeeded` or `failed`. A failed one is an incident |
| `apchi_applies_interrupted_total` | `kind` | Applies a restart of Apchi ended, counted by the process that recovered them. They are counted in `apchi_applies_total` too, but get no durations, because those would include the downtime |

**Apchi itself.**

| Metric | Labels | |
| --- | --- | --- |
| `apchi_certificate_expiry_timestamp_seconds` | `path` | When each certificate Apchi *loaded* expires: those it presents and the CAs it trusts. It reports what was loaded, not what is mounted now, because after a rotation Apchi keeps presenting the old certificate until it restarts |
| `apchi_info` | `version` | Always 1 |
| `process_*`, `python_info` | | The Prometheus client's own |

## Alerts worth having

```yaml
groups:
  - name: apchi
    rules:
      # Operators are locked out until an Admin releases it. After a failed Auto Rollback this
      # is the only signal that does not need someone to open the UI.
      - alert: ApchiMaintenanceMode
        expr: max(apchi_maintenance_mode) == 1
        for: 5m

      - alert: ApchiIncident
        expr: sum(increase(apchi_applies_total{outcome="incident"}[15m])) > 0

      # Apchi presents what it loaded until it restarts, so renewing the Secret is not
      # enough: the pod has to restart too.
      - alert: ApchiCertificateExpiring
        expr: min by (path) (apchi_certificate_expiry_timestamp_seconds) - time() < 14 * 86400
```

A failed Apply is not worth alerting on by itself. Its Auto Rollback has already put the
Cluster back, and the Operator who started it sees why on the Apply.

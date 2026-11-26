# Argo CD

The chart installs through an Argo CD Application with no `ignoreDifferences` and no sync
options. That includes automated sync with `prune` and `selfHeal`. `tests/argo` runs this setup
in CI against the real chart and a real Argo CD.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata: {name: apchi, namespace: argocd}
spec:
  project: default
  source:
    repoURL: https://git.example.com/platform/trino.git
    targetRevision: main
    path: charts/apchi
    helm:
      valuesObject: {}   # your values
  destination: {server: https://kubernetes.default.svc, namespace: trino}
  syncPolicy:
    automated: {prune: true, selfHeal: true}
```

## What Apchi writes, and why Argo CD leaves it alone

Apchi writes to two kinds of objects. Neither gives Argo CD a reason to sync.

**Secrets the chart doesn't render.** These hold the catalog seed, the access-control rules,
the user-mapping file, the client certificates, Resource Groups, the Raw Files, and each role's
Server Configuration. Apchi creates any that are missing when it starts. No release owns them,
so Argo CD neither displays nor prunes them. They are also why the chart creates none of them
itself: Argo CD runs Helm hooks on every sync, and a hook-created Secret was reset to the
chart's starting contents each time. See
[the chart's README](../reference/apchi-chart.md#the-secrets-apchi-writes-and-why-the-chart-creates-none-of-them).

**Three things on the Trino Deployments, which the chart does render:**

- the volume and mount for `resource-groups.properties`;
- the volumes and mounts for the Admin's Raw Files;
- the `apchi.dev/restarted-at` and `apchi.dev/restarted-because` annotations on the pod
  template.

The chart renders none of these entries. Argo CD's diff therefore leaves them out: the
Application stays `Synced` with them in place, and self-heal doesn't remove them. A sync for a
change to the chart re-applies the Deployment and keeps them, too.

The Event Listener doesn't touch the Deployment at all. The chart always mounts the
coordinator's Server Configuration Secret at `/etc/trino/apchi`, and the listener is a file in
that Secret (ADR-0011).

## What was measured, and what wasn't

**Measured** with Argo CD v2.13.3 on kind, using its default diff and automated sync with
`prune` and `selfHeal`:

1. Apchi applied a catalog, a grant, Resource Groups, a Raw File and an Event Listener.
2. A change to the coordinator's pod template in the chart then synced and restarted the
   coordinator.
3. Every Secret Apchi had written came through that sync byte for byte, and Apchi's volumes were
   still on the Deployment.
4. The Application returned to `Synced` and `Healthy`, and the restarted coordinator still served
   the catalog.

**Not measured:**

- the `ServerSideApply=true` sync option;
- server-side diff;
- Argo CD 3.x.

Each of these changes how Argo CD compares and applies the Deployment. If you use any of them,
check that the Application stays `Synced` after an Apply that adds a Raw File. If it doesn't,
the narrow fix is to ignore only what Apchi writes. With the default names, every volume and
mount Apchi adds starts with `apchi-`. If you renamed them through the chart's `volumes.*`
values, match those names instead:

```yaml
spec:
  ignoreDifferences:
    - group: apps
      kind: Deployment
      jqPathExpressions:
        - '.spec.template.spec.volumes[] | select(.name | startswith("apchi-"))'
        - '.spec.template.spec.containers[].volumeMounts[] | select(.name | startswith("apchi-"))'
        - '.spec.template.metadata.annotations["apchi.dev/restarted-at"]'
        - '.spec.template.metadata.annotations["apchi.dev/restarted-because"]'
  syncPolicy:
    syncOptions: [RespectIgnoreDifferences=true]
```

`RespectIgnoreDifferences` is what stops a sync from removing them. Without it, the ignored
fields only stop showing as a difference.

## Values Argo CD can't work out for you

Argo CD renders the chart without access to the cluster, so Helm's `lookup` finds nothing:

- **A `reencrypt` Route** takes the CA to verify Trino with from `destinationCACertificate`. Set
  it there, because it can't be read from `caBundle`.
- **`expose.*.kind: auto`** still works: Argo CD passes the target cluster's API versions to the
  chart.

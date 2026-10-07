# Trino chart

A Trino cluster Apchi can configure.

```sh
helm install trino charts/trino -n trino
helm install apchi oci://ghcr.io/infinityhub123/charts/apchi -n trino \
  --set trino.host=trino \
  --set trino.coordinatorDeployment=trino-coordinator \
  --set trino.workerDeployment=trino-worker \
  --set mongodb.uri=mongodb://your-mongo:27017
```

## Why this exists

Apchi does not own your Trino deployment and this chart does not change that — §16 is
deliberate about asserting preconditions rather than owning manifests, because heap sizes
and node selectors belong to Admins. What it does is make the handful of things Apchi
*does* require impossible to get wrong, since each of them fails in a way that does not
look like its cause.

The official `trino/trino` chart is the general-purpose one and has far more in it. It does
not work with Apchi yet, for a reason worth knowing even if you use this chart: it mounts
its whole configuration directory from one ConfigMap, and Apchi mounts a single file inside
`/etc/trino`. A `subPath` mount at a path inside a ConfigMap directory mount fails the
container outright — runc has no file to bind onto, and the pod crashloops with no Trino
log at all. See #93.

This chart mounts each configuration file separately, which is why it works.

## What it guarantees

`scripts/check_trino_chart.py` renders this chart and runs **Apchi's own precondition
checker** against the result, so the chart cannot drift into something Apchi refuses. CI
runs it. These are the five things it proves:

1. **No Apchi-managed file is `subPath`-mounted.** Kubernetes documents that a `subPath`
   mount never receives updates — the file is frozen at pod creation, permanently — so
   Apchi would rewrite a Secret, see the write succeed, report success, and Trino would
   never see it. The access-control rules and the certificate directory are whole-volume
   mounts for that reason.
2. **The catalog seed initContainer exists.** It copies the durable catalogs into the
   writable store before Trino reads them. Without it the Cluster comes up with no
   catalogs at all.
3. **Nothing read-only is mounted over the catalog store.** Trino writes that directory
   itself, and Secret and ConfigMap volumes are always read-only, so every
   `CREATE CATALOG` would fail. An `emptyDir` is what belongs there.
4. **Nothing but Apchi is mounted at the paths Apchi owns.** Two volumes over one file
   means whichever wrote last wins, silently.
5. **`security.refresh-period` is set.** Without it Trino reads the access-control rules
   once at startup and never again.

## The bootstrap, and why `helm upgrade` is safe

Trino reads the access-control rules and the user-mapping file while loading and refuses to
boot without either, so the Cluster cannot wait for Apchi to write them. This chart creates
all six Secrets Apchi writes into — but as a **`pre-install` hook**, which runs once and is
not part of the release.

That is the whole design, and it matters: a Secret tracked in the release would be reverted
by the next `helm upgrade`, so an unrelated chart bump would silently undo a permission
change. Verified on a live cluster — after Apchi had applied catalogs, grants and resource
groups, a `helm upgrade` that changed the worker count and then one that changed the
chart's own volume list both left every Secret and Apchi's own pod-template volume exactly
as Apchi had them.

`helm uninstall` leaves those Secrets behind, which is also correct: they hold the
Cluster's configuration and, for the catalog seed, its only durable copy. Remove them
deliberately or not at all.

Set `bootstrap.create=false` when the Secrets already exist.

## Values you are likely to set

| Value | Default | Notes |
| --- | --- | --- |
| `worker.replicas` | `2` | The coordinator is always one — Trino cannot drain one, so a restart destroys every running query (ADR-0003) |
| `image.tag` | chart `appVersion` (`483`) | Apchi is developed and tested against 483 |
| `trino.environment` | `production` | `node.environment`; lowercase alphanumeric and underscores only |
| `trino.jvm.maxHeapSize` | `2G` | Per node |
| `trino.securityRefreshPeriod` | `30s` | How soon a permission change takes effect. Lower is a faster Apply and more file reads |
| `authentication.type` | `insecure` | `certificate` for production. Both feed the same file to the same parser; Trino rejects the certificate variant unless certificate authentication is configured (§7.6) |
| `fullnameOverride` | `""` | Pins the Deployment names Apchi is configured with, independently of the release name |
| `trino.additionalCoordinatorProperties` | `[]` | Verbatim into `config.properties`. Properties Apchi owns are not yours to set here, and the preconditions will say so |
| `bootstrap.catalogs` | `{}` | Catalogs to come up with before Apchi has applied. Useful for a demo and nothing else: Apchi's first Apply rewrites the seed, so anything here that Apchi does not know about disappears at the following restart |

`coordinator.*` and `worker.*` also take `resources`, `nodeSelector`, `tolerations`,
`affinity` and `podAnnotations`.

## What this chart does not do

**TLS and authentication.** `authentication.type: certificate` selects which authenticator
reads the user-mapping file; it does not configure TLS, a keystore or a truststore. Those
go through `trino.additionalCoordinatorProperties` and mounts of your own.

**Autoscaling, ingress, exchange managers, JMX export, Kafka schemas, group providers.**
The official chart has all of these. If you need them, that is the chart to use — and #93
is what has to land first.

**Resource groups and event listeners.** Not an omission: Apchi owns those files and adds
the volumes for them to this pod template itself, because for both of them the *absence* of
the mount is how "none configured" is expressed. Trino refuses to start if a file it was
told to read is missing.

## Verified

Against minikube with Trino 483: the cluster comes up, Apchi installs beside it and passes
its preconditions, a catalog applies through DDL and appears in `SHOW CATALOGS`, a resource
group and a selector apply with a Rollout and `system.runtime.queries` then reports queries
landing in `adhoc`, permissions apply without a restart, and two `helm upgrade`s leave all
of it intact.

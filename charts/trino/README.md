# Trino chart

A Trino cluster Apchi can configure.

```sh
# Apchi first: it creates the Secrets Trino mounts, and Trino will not boot without two.
helm install apchi oci://ghcr.io/infinityhub123/charts/apchi -n trino \
  --set trino.host=trino \
  --set trino.coordinatorDeployment=trino-coordinator \
  --set trino.workerDeployment=trino-worker \
  --set mongodb.uri=mongodb://your-mongo:27017

helm install trino charts/trino -n trino --set fullnameOverride=trino
```

## Why this exists

Apchi does not own your Trino deployment and this chart does not change that — §16 is
deliberate about asserting preconditions rather than owning manifests, because heap sizes
and node selectors belong to Admins. What it does is make the handful of things Apchi
*does* require impossible to get wrong, since each of them fails in a way that does not
look like its cause.

The official `trinodb/charts` chart is the general-purpose one and has far more in it. It
**does** work with Apchi, with two settings and a handful of values —
[`docs/official-trino-chart.md`](../../docs/official-trino-chart.md) is the whole recipe, and
CI proves Apchi's preconditions pass against what that chart renders.

The difference between the two is worth knowing even if you use this one. The official chart
mounts its whole configuration directory from one ConfigMap, and Kubernetes will not place a
single file inside a read-only directory volume: runc has no file to bind onto, the container
fails with `not a directory`, and the pod crashloops with no Trino log at all. A nested
*directory* mount in the same position is fine.

This chart mounts each configuration file separately instead, which is the shape that lets
Apchi own the two mounts Trino pins to `/etc/trino` — `resource-groups.properties` and
`event-listener.properties`. On the official chart the deployment provides those two pointers
and Apchi fills the directories they name, which costs the ability to empty Event Listeners
and rules out Admin raw files. Neither limit applies here.

## What it guarantees

`scripts/check_trino_chart.py` renders this chart and runs **Apchi's own precondition
checker** against the result, so the chart cannot drift into something Apchi refuses. CI
runs it. These are the things it proves:

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
6. **No read-only directory volume covers a path Apchi mounts a single file at.** The
   `not a directory` failure above, refused before an Apply rather than discovered during
   one. This is the check the official chart fails without its own configuration.

The checker runs a seventh — **a pointer the deployment claims to provide is really there**,
and really names the directory Apchi fills — but it has nothing to say here. That check only
engages on a deployment where the Admin owns those pointers, and this chart is not one: it
runs against default settings, where Apchi mounts both files itself. The check matters for the
official chart, not for this one.

## This chart creates no Secrets

It creates none, and mounts four: the access-control rules, the user-mapping file, the catalog
seed and the client certificates. They hold configuration Apchi writes and rewrites on every
apply, so they are Apchi's data and Apchi's chart creates them — which is why Apchi is
installed first. Trino reads the first two while loading and refuses to boot when either is
missing, so it cannot start and wait; Apchi has no such problem, because its readiness depends
on MongoDB and nothing else.

Apchi's chart creates **six**. The other two — resource groups and the event listener — are
not mounted here on purpose: for both of them the *absence* of the mount is how "none
configured" is expressed, since Trino refuses to start when a file it was told to read is
missing. So Apchi adds and removes those two mounts itself, and they are the only part of this
pod template it writes.

Keeping them out of here is what lets this chart be read, reviewed and released without
Apchi in the picture. The cost is that the two charts must now **agree on names**, and
nothing reconciles a mismatch: Apchi would write a Secret nobody reads and report a
successful apply the Cluster never saw. `scripts/check_trino_chart.py` renders both charts
and checks exactly that, because while they were one chart it could not happen and now it
can.

## Values you are likely to set

| Value | Default | Notes |
| --- | --- | --- |
| `worker.replicas` | `2` | The coordinator is always one — Trino cannot drain one, so a restart destroys every running query (ADR-0003) |
| `image.tag` | chart `appVersion` (`483`) | Apchi is developed and tested against 483 |
| `trino.environment` | `production` | `node.environment`; lowercase alphanumeric and underscores only |
| `trino.jvm.maxHeapSize` | `2G` | Per node |
| `trino.securityRefreshPeriod` | `30s` | How soon a permission change takes effect. Lower is a faster Apply and more file reads |
| `authentication.type` | `insecure` | `certificate` for production. Both feed the same file to the same parser; Trino rejects the certificate variant unless certificate authentication is configured (§13.3) |
| `fullnameOverride` | `""` | Pins the Deployment names Apchi is configured with, independently of the release name |
| `trino.additionalCoordinatorProperties` | `[]` | Verbatim into `config.properties`. Properties Apchi owns are not yours to set here, and the preconditions will say so |

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

**The Secrets Apchi writes.** `charts/apchi` creates those, including the two Trino needs
before it can boot. Install it first.

## Verified

Against minikube with Trino 483: the cluster comes up, Apchi installs beside it and passes
its preconditions, a catalog applies through DDL and appears in `SHOW CATALOGS`, a resource
group and a selector apply with a Rollout and `system.runtime.queries` then reports queries
landing in `adhoc`, permissions apply without a restart, and two `helm upgrade`s leave all
of it intact.

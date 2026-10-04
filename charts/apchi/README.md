# Apchi Helm chart

Installs Apchi into the namespace its Trino runs in.

```sh
helm install apchi oci://ghcr.io/infinityhub123/charts/apchi --version 0.1.0 \
  -n trino --set mongodb.uri=mongodb://your-mongo:27017
```

From a clone, `helm install apchi charts/apchi` with the same values.

Apchi is one container with no state of its own, so the chart is a Deployment, a Service, a
ServiceAccount and a namespaced Role. It installs nothing into Trino and watches nothing
outside its namespace.

To try it with nothing set up, `./scripts/quickstart.sh` from the repository root brings up a
Trino, this chart and a MongoDB on minikube or kind in one command.

## What you have to set

**`mongodb.uri`**, or `mongodb.deploy=true`. Apchi keeps every Snapshot and all in-flight
state in MongoDB, so the install fails rather than starting an Apchi that cannot keep a
Snapshot. `mongodb.deploy=true` runs a single-replica MongoDB on an `emptyDir` beside Apchi —
fine for trying Apchi out, and it discards every Snapshot when its pod restarts.

**The names of your Trino deployment**, if they are not the defaults. The chart assumes a
Service called `trino` and Deployments called `trino-coordinator` and `trino-worker`.

```yaml
trino:
  host: trino-coordinator-svc
  coordinatorDeployment: my-trino-coordinator
  workerDeployment: my-trino-worker
```

**The Secret names**, if your Trino mounts different ones. This is the one place a
disagreement is silent and damaging: Apchi writes these Secrets and Trino reads them, and
nothing reconciles a mismatch — Apchi reports a successful apply and the coordinator never
sees it. `deploy/trino-dev/` mounts exactly the defaults.

```yaml
secrets:
  catalogSeed: trino-catalog-seed
  accessControl: trino-access-control
  eventListener: trino-event-listener
  certificateMapping: trino-user-mapping
  clientCertificates: trino-client-certificates
  resourceGroups: trino-resource-groups
```

## Values

| Value | Default | What it is |
| --- | --- | --- |
| `image.repository` | `ghcr.io/infinityhub123/apchi` | Published on every release for `linux/amd64` and `linux/arm64` |
| `image.tag` | `""` | Empty means the chart's `appVersion`, so an install gets the image that chart version was released with rather than whatever `latest` is today |
| `replicaCount` | `1` | Leave it. An Apply is in-process state, so a second Apchi runs a second pipeline against one Trino |
| `environment` | `np` | `np`/`test`/`prep`/`prod`. Decides the log level and format: DEBUG and console in np and test, INFO and JSON in prep and prod |
| `logging.level`, `logging.json` | `null` | Override the above. An explicit level can be raised during an incident without a rebuild |
| `trino.host`, `trino.port` | `trino`, `8080` | The coordinator's Service. Apchi issues catalog DDL and the smoke query through it |
| `trino.user` | `apchi` | The identity Apchi issues DDL as, and the only one granted `owner` on catalogs |
| `trino.coordinatorDeployment` | `trino-coordinator` | Patched by a Rollout; Validation reads its image so the probe matches the Cluster |
| `trino.workerDeployment` | `trino-worker` | Its ready replica count is what Verification expects to register |
| `trino.containerName` | `trino` | The container within those pods |
| `trino.catalogStoreDir` | `/data/trino/catalogs` | Must match Trino's `catalog.config-dir`. Nothing read-only may be mounted here or above it |
| `trino.verificationCatalog` | `system` | What the smoke query reads |
| `mongodb.uri` | — | Required unless `mongodb.deploy` |
| `mongodb.database` | `apchi` | |
| `mongodb.deploy` | `false` | A MongoDB beside Apchi, for trying it out. Loses Snapshots on restart |
| `secrets.*` | see above | Must be what Trino mounts |
| `volumes.*` | `apchi-*` | The volumes Apchi adds to the coordinator's pod template. Everything else there is the Admin's |
| `timeouts.rolloutSeconds` | `600` | A coordinator that never comes back fails the Apply rather than hanging it |
| `timeouts.validationSeconds` | `300` | The hard limit on Validation. Also sets the liveness budget, so a Validation is never killed mid-Apply |
| `certificates.expiringWithinDays` | `30` | When Apchi starts calling a Client Certificate expiring. Renewal is manual |
| `certificates.maxBytes` | `1048576` | What a Kubernetes Secret may hold; a larger upload is refused with that reason |
| `alertWebhookUrl` | `""` | Where a failed Auto Rollback is reported. Empty means log only |
| `extraEnv` | `[]` | `APCHI_`-prefixed environment, for a setting newer than this chart |
| `rbac.create` | `true` | `false` to bind your own Role. Without an equivalent one every Apply fails at the API server |
| `serviceAccount.create`, `.name`, `.annotations` | `true`, `""`, `{}` | |
| `service.type`, `service.port` | `ClusterIP`, `8000` | |
| `resources`, `nodeSelector`, `tolerations`, `affinity`, `podAnnotations`, `podLabels` | | The usual |

`scripts/check_chart_env.py` renders this chart and builds Apchi's own `Settings` out of the
result, so a value the application does not read, or a value it will not parse, fails CI
rather than becoming a pod that will not start. CI runs it.

## What the Role grants

Every verb is one Apchi calls; `templates/rbac.yaml` says why for each. Namespaced, with no
cluster-scoped rule.

| Resource | Verbs | Why |
| --- | --- | --- |
| `secrets` | get, list, create, patch, delete | The generated configuration. Create/delete is a Validation's ephemeral Secret; list is how a crashed Apchi finds ones it leaked |
| `configmaps` | get | One read-only check: whether the access-control properties carry a refresh period |
| `pods`, `pods/log` | get, list, create, delete / get | The ephemeral validation coordinator. Its log is the only place a refusing Trino explains itself |
| `deployments` | get, patch | A Rollout. Apchi never creates or deletes one: it does not own the Trino deployment |

## Not covered by this chart

**Authentication.** Apchi has none yet — every endpoint is open, the Admin ones included.
Install it where only platform staff can reach it.

**Trino.** Apchi configures a Trino somebody else deployed. `deploy/trino-dev/` is a reference
deployment that satisfies Apchi's preconditions; §7.1 and §16 of `apchi_implementation.md` are
the requirements themselves.

**MongoDB, properly.** `mongodb.deploy` is for trying Apchi out. MongoDB is the single copy of
every Snapshot (§18), so a real install points `mongodb.uri` at something backed up.

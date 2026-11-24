# Apchi Helm chart

Installs Apchi and the Trino cluster it configures, as one release.

Published to GHCR on every tag:

```sh
helm install apchi oci://ghcr.io/infinityhub123/charts/apchi --version 0.3.0 \
  -n trino --set mongodb.uri=mongodb://your-mongo:27017
```

Or from a clone, while changing Apchi itself:

```sh
helm install apchi charts/apchi -n trino --set mongodb.uri=mongodb://your-mongo:27017
```

One release holds Apchi (a Deployment, a Service, a ServiceAccount and a namespaced Role), the
six Secrets Apchi writes configuration into, and Trino: one coordinator, its workers and their
Service. Apchi is configured from the same helpers that name the Trino Deployments, so the two
cannot disagree. The Secrets are install hooks, so they exist before the coordinator starts --
Trino reads two of them while loading and refuses to boot when either is missing.

To try it with nothing set up, `./scripts/quickstart.sh` from the repository root brings up
this chart and a MongoDB on minikube or kind in one command.

## What you have to set

**`mongodb.uri`**, or `mongodb.deploy=true`. Apchi keeps every Snapshot and all in-flight
state in MongoDB, so the install fails rather than starting an Apchi that cannot keep a
Snapshot. `mongodb.deploy=true` runs a single-replica MongoDB on an `emptyDir` beside Apchi —
fine for trying Apchi out, and it discards every Snapshot when its pod restarts.

**Nothing about Trino's names.** Its Deployments are `<trino.name>-coordinator` and
`<trino.name>-worker` and its Service `<trino.name>`, `trino` by default, and Apchi is told
exactly those. The Secret names under `secrets.*` are likewise created and mounted by this one
chart; `scripts/check_trino_chart.py` fails CI if the coordinator would mount one the chart
does not create.

## Values

| Value | Default | What it is |
| --- | --- | --- |
| `image.repository` | `ghcr.io/infinityhub123/apchi` | Published on every release for `linux/amd64` and `linux/arm64` |
| `image.tag` | `""` | Empty means the chart's `appVersion`, so an install gets the image that chart version was released with rather than whatever `latest` is today |
| `image.pullPolicy`, `imagePullSecrets` | `IfNotPresent`, `[]` | |
| `replicaCount` | `1` | Leave it. An Apply is in-process state, so a second Apchi runs a second pipeline against one Trino |
| `auth.mode` | `none` | `none`/`password`/`sso`. **`none` means every caller is treated as an Admin**, which is what Apchi has always done |
| `auth.initialAdmin.username` | `admin` | The one Admin a new install starts with, in `password` mode |
| `auth.initialAdmin.passwordHash` | `""` | An Argon2id **hash**, never the password. Applied only when Apchi has no users, so it cannot resurrect a deleted account or reset a changed password |
| `auth.initialAdmin.existingSecret`, `.existingSecretKey` | `""`, `password-hash` | A Secret holding that hash, preferred over the value above: the hash is offline-crackable, so it is worth keeping out of the release |
| `auth.sessionHours` | `12` | How long an untouched session lasts. Logout and disabling a user take effect at once regardless |
| `auth.oidc.issuer` | `""` | Required in `sso` mode. Every endpoint is discovered from here, so a provider that rotates keys or moves an endpoint keeps working |
| `auth.oidc.clientId` | `""` | Required in `sso` mode |
| `auth.oidc.redirectUrl` | `""` | Required in `sso` mode, and the one value nothing can guess: the URL a **browser** reaches Apchi on, registered with the provider verbatim. A wrong one is a login loop with no error anywhere |
| `auth.oidc.existingSecret`, `.existingSecretKey` | `""`, `client-secret` | A Secret holding the client secret, preferred over `auth.oidc.clientSecret`, which puts it in the release and in `helm get values` |
| `auth.oidc.scopes` | `openid profile email groups` | `groups` is not a standard OIDC scope and some providers reject one they do not know. Trim it if yours does |
| `auth.oidc.usernameClaim` | `preferred_username` | The person's Apchi username. Not `sub`, which providers routinely make an opaque blob, and the audit records an actor somebody must be able to read. It follows that a provider which *changes* a login name produces a new Apchi user |
| `auth.oidc.roleClaim`, `auth.oidc.adminGroups` | `groups`, `""` | Everyone who authenticates is an Operator; a member of one of `adminGroups` is an Admin. Empty means **nobody** is an Admin, which closes `/admin/*` rather than opening it |
| `audit.retentionDays` | `""` | How long §20's audit history is kept. **Empty means forever, deliberately** — a trail that deletes itself is worse than one that grows. Setting it creates a TTL index, so history past it is discarded |
| `candidateMaxBytes` | `15728640` | The most the Configuration Candidate may occupy. It is **one** MongoDB document, which MongoDB caps at 16MB, so this is a ceiling on all six Sections together rather than a per-Section quota. Below 16MB to leave room for BSON overhead and the Snapshot an Apply makes of the same content |
| `logging.level`, `logging.json` | `null` | INFO and console lines when null. An explicit level can be raised during an incident without a rebuild |
| `environment` | `null` | **Deprecated**, removed in the next release. While set, `np`/`test` still mean DEBUG and `prep`/`prod` INFO and JSON, and Apchi logs a warning. Any value is accepted |
| `logging.debug` | `false` | Debugging mode: DEBUG unless `logging.level` says otherwise, each line naming the logger and line it came from, and every call to Kubernetes, Trino and MongoDB logged by name and timing. Payloads never, so it is safe to turn on in production. Dependencies' own debug output stays off, because PyMongo's prints Snapshots and the Kubernetes client's prints Secrets |
| `trino.url` | `""` | The coordinator, scheme included; empty means `http://<trino.name>:8080`, the Service this chart creates. Set it for a coordinator serving HTTPS. A URL and not a host plus a port because the Trino client reads `https` from a port only at exactly 443, so `8443` silently meant plaintext |
| `trino.tls.existingSecret` | `""` | A Secret holding the PEM files below, mounted read-only at `trino.tls.mountPath`. Required to reach a coordinator that serves HTTPS |
| `trino.tls.certificateKey`, `trino.tls.keyKey` | `""`, `""` | The certificate Apchi authenticates with, where the coordinator sets `http-server.authentication.type=CERTIFICATE`. Both or neither |
| `trino.user` | `apchi` | The identity Apchi issues DDL as, and the only one granted `owner` on catalogs |
| `trino.name` | `trino` | Names the Service and the `-coordinator` and `-worker` Deployments, and configures Apchi with the same |
| `trino.image.repository`, `trino.image.tag` | `trinodb/trino`, `483` | Apchi is developed and tested against 483. Validation runs the coordinator's image, whatever it is |
| `trino.worker.replicas` | `2` | The coordinator is always one — Trino cannot drain one, so a restart destroys every running query (ADR-0003) |
| `trino.coordinator.resources`, `trino.worker.resources`, `trino.coordinator.seedResources` | `{}` | Requests and limits for the two roles and for the catalog-seeding initContainer. Each renders nothing when empty |
| `trino.coordinator.*`, `trino.worker.*` | | Also `nodeSelector`, `tolerations`, `affinity`, `podAnnotations` and `readinessProbe` |
| `trino.environment` | `production` | `node.environment`; lowercase alphanumeric and underscores only |
| `trino.jvm.maxHeapSize` | `2G` | Per node |
| `trino.fsGroup` | `1000` | The group that owns the credential volumes, which are `0640`. On OpenShift's `restricted-v2` it must be a group the namespace allows -- the first of its `openshift.io/sa.scc.supplemental-groups` annotation -- or the pods are refused. `null` renders none, which Apchi accepts only with `restrictedPodSecurity` set. See [OpenShift and other restricted clusters](../../docs/deploying/openshift.md) |
| `trino.securityRefreshPeriod` | `30s` | How soon a permission change takes effect. Lower is a faster Apply and more file reads |
| `trino.authentication.type` | `insecure` | `certificate` for production. Both feed the same file to the same parser; Trino rejects the certificate variant unless certificate authentication is configured (§13.3) |
| `trino.additionalCoordinatorProperties`, `trino.additionalWorkerProperties` | `[]` | Verbatim into `config.properties`. Properties Apchi owns are not yours to set here, and the preconditions will say so |
| `trino.catalogStoreDir` | `/data/trino/catalogs` | Where Trino keeps its dynamic catalog store, inside its writable data directory `/data/trino` |
| `trino.verificationCatalog` | `system` | What the smoke query reads |
| `mongodb.uri` | — | Required unless `mongodb.deploy`. With `mongodb.tls` set it may carry `?authMechanism=MONGODB-X509`, which is worth preferring: the URI then holds no secret at all, where a SCRAM password in it sits in `helm get values` and the pod spec |
| `mongodb.database` | `apchi` | |
| `mongodb.tls.existingSecret` | `""` | A Secret holding the PEM files below, mounted read-only at `mongodb.tls.mountPath`. Required to reach a MongoDB that requires TLS |
| `mongodb.tls.certificateKeyKey` | `""` | One PEM holding Apchi's certificate **and its key** — PyMongo's `tlsCertificateKeyFile` shape. Either key alone turns TLS on |
| `mongodb.tls.certificateKey`, `mongodb.tls.keyKey` | `""`, `""` | Or the certificate and its key as two keys of the Secret, as most issuers write them (`tls.crt`, `tls.key`). Apchi joins them into one private file as it starts, whatever newline either ends with. Both or neither, and not alongside `certificateKeyKey` |
| `pod.caBundle.existingSecret` or `.configMap`, `.key`, `.mountPath` | `""`, `""`, `ca.crt`, `/etc/apchi/ca` | A CA to trust **in addition to** the public store, for every outbound TLS connection Apchi makes — the coordinator, MongoDB, the OIDC provider and the alert webhook. One bundle, because an org with an internal PKI has one root for all four. Mounted unrestricted and with no `fsGroup`: public certificates, no key. A ConfigMap for OpenShift's injected trusted-CA bundle (key `ca-bundle.crt`); named rather than looked up, because Helm's `lookup` finds nothing under `helm template` or Argo CD |
| `restrictedPodSecurity` | `false` | Set on a cluster that forbids pinning `fsGroup` and runs each pod as an arbitrary uid -- OpenShift's `restricted-v2` SCC. A mounted file then reaches the process only by being world-readable, so Apchi's pod renders no `fsGroup` and mounts its credentials `0644`; the Validation pod Apchi creates carries no `fsGroup` either; and Apchi accepts `0644` credential mounts on the Trino pods instead of refusing them, while still refusing a writable one. The rest of the security context -- `runAsNonRoot`, seccomp, capabilities, the uid -- the platform adds itself, and the image runs as any uid. See [OpenShift and other restricted clusters](../../docs/deploying/openshift.md). The bundled MongoDB (`mongodb.deploy`) is for trying Apchi out and is not built for a restricted cluster |
| `pod.fsGroup` | `10001` | The group every mounted credential is owned by, which must be the one Apchi's process runs as. Ignored under `restrictedPodSecurity`. A Secret volume's files are root-owned whatever their mode, so a wrong value here is a key Apchi cannot read |
| `pod.credentialFileMode` | `0640` | Not `0600`: the kubelet ORs `0440` into every file in a read-only volume, so `0600` cannot be produced. `0644` would leave a private key world-readable. Ignored under `restrictedPodSecurity`, where `0644` is the only mode an arbitrary uid can read |
| `mongodb.deploy`, `mongodb.image` | `false`, `mongo:7` | A MongoDB beside Apchi, for trying it out. Loses Snapshots on restart |
| `secrets.*` | see above | Must be what Trino mounts |
| `volumes.*` | `apchi-*` | The volumes Apchi adds to the coordinator's pod template. Everything else there is the Admin's. The user-mapping Secret is not here: the Admin mounts it as a directory and Apchi only fills it |
| `deployment.providesResourceGroupsProperties` | `false` | Set on a coordinator that mounts the whole of `/etc/trino`, where Apchi cannot mount `resource-groups.properties` itself. The deployment then provides it, pointing `resource-groups.config-file` at `/etc/trino/resource-groups/` |
| `deployment.setsEventListenerConfigFiles` | `false` | The same, for `event-listener.config-files` naming a file in `/etc/trino/event-listener/`. Costs the ability to empty that Section, because Trino refuses to start without the file the property names |
| `validation.resources` | `{}` | Requests and limits for the Validation pod's container. Apchi creates that pod itself, so this is the only place a cluster that requires them on every container can set them. Trino needs about `2Gi` to start. Empty sets none |
| `validation.catalogConnectivityBlocksApply` | `false` | Whether a Catalog the validation probe cannot reach **fails** the Apply rather than warning on it. Off by default because the probe's network is not the coordinator's: a firewall between the probe and a database would otherwise block every Apply for a configuration that works. Turn it on where the two are known to reach the same places (§6) |
| `timeouts.rolloutSeconds` | `600` | A coordinator that never comes back fails the Apply rather than hanging it |
| `timeouts.validationSeconds` | `300` | The hard limit on Validation. Also sets the liveness budget, so a Validation is never killed mid-Apply |
| `certificates.expiringWithinDays` | `30` | When Apchi starts calling a certificate expiring. Both a Client Certificate, where it sets the status an Operator filters on, and the certificates Apchi itself presents, which the Apply path then warns about in the log. Renewal is manual |
| `certificates.maxBytes` | `1048576` | What a Kubernetes Secret may hold; a larger upload is refused with that reason |
| `alertWebhookUrl` | `""` | Where a failed Auto Rollback is reported. Empty means log only |
| `extraEnv` | `[]` | `APCHI_`-prefixed environment, for a setting newer than this chart. A name Apchi does not read stops it starting, naming the variable -- which is the point: a misspelt one would otherwise leave it running on a default |
| `bootstrap.create` | `true` | Creates the six Secrets Apchi writes into, as a `pre-install` hook. `false` when they already exist |
| `bootstrap.catalogs` | `{}` | Catalogs the Cluster comes up with before Apchi has applied. For a demo and nothing else: Apchi's first apply rewrites the seed, so anything here Apchi does not know about disappears at the following restart |
| `rbac.create` | `true` | `false` to bind your own Role. Without an equivalent one every Apply fails at the API server |
| `serviceAccount.create`, `.name`, `.annotations` | `true`, `""`, `{}` | |
| `service.type`, `service.port` | `ClusterIP`, `8000` | |
| `resources`, `nodeSelector`, `tolerations`, `affinity`, `podAnnotations`, `podLabels` | | The usual |

**Rotating anything under `trino.tls`, `mongodb.tls` or `pod.caBundle` needs a
`kubectl rollout restart`.** Apchi reads all of it once, at startup, which is what keeps the
coordinator's two client libraries from presenting different certificates mid-rotation; the
next Apply logs a warning naming a file that changed, and each certificate's expiry is logged
as it loads. §18 argues it and #193 has the measurements.

`scripts/check_chart_env.py` renders this chart and builds Apchi's own `Settings` out of the
result, so a value the application does not read, or a value it will not parse, fails CI
rather than becoming a pod that will not start. CI runs it.

## What the Role grants

Every verb is one Apchi calls; `templates/rbac.yaml` says why for each. Namespaced, with no
cluster-scoped rule.

| Resource | Verbs | Why |
| --- | --- | --- |
| `secrets` | get, list, create, patch, delete | The generated configuration. Create/delete is a Validation's ephemeral Secret; list is how a crashed Apchi finds ones it leaked |
| `configmaps` | get, create, patch | Two things. Read-only: whether the access-control properties carry a refresh period. Written: the high-water mark, which lives in a ConfigMap *outside* MongoDB so that an empty `snapshots` collection can be told apart from a new install (§15, §18) |
| `pods`, `pods/log` | get, list, create, delete / get | The ephemeral validation coordinator. Its log is the only place a refusing Trino explains itself |
| `deployments` | get, patch | A Rollout. Apchi never creates or deletes one: it does not own the Trino deployment |

## The bootstrap, and why `helm upgrade` is safe

Trino reads the access-control rules and the user-mapping file while loading and refuses to
boot without either, so the Cluster cannot wait for Apchi to write them. This chart creates
all six Secrets Apchi writes into — but as a **`pre-install` hook**, which runs once and is
not part of the release.

That is the whole design, and it matters: a Secret tracked in the release would be reverted
by the next `helm upgrade`, so an unrelated chart bump would silently undo a permission
change. Verified on a live cluster — after Apchi had applied catalogs, grants and resource
groups, a `helm upgrade` that changed the worker count and then one that changed a chart's
own volume list both left every Secret and Apchi's own pod-template volume exactly as Apchi
had them.

`helm uninstall` leaves those Secrets behind, which is also correct: they hold the Cluster's
configuration and, for the catalog seed, its only durable copy. Remove them deliberately or
not at all.

## The Trino cluster

`scripts/check_trino_chart.py` renders this chart and runs **Apchi's own precondition
checker**, configured as the chart configures Apchi, against the Trino it renders -- so the
chart cannot drift into something Apchi refuses. CI runs it. It proves:

1. **No Apchi-managed file is `subPath`-mounted.** A `subPath` mount never receives updates,
   so Apchi would rewrite a Secret, see the write succeed, and Trino would never see it. The
   access-control rules and the certificate directory are whole-volume mounts for that reason.
2. **The catalog seed initContainer exists.** It copies the durable catalogs into the
   writable store before Trino reads them. Without it the Cluster comes up with no catalogs.
3. **Nothing read-only is mounted over the catalog store.** Trino writes that directory
   itself, so every `CREATE CATALOG` would fail. An `emptyDir` is what belongs there.
4. **Nothing but Apchi is mounted at the paths Apchi owns.** Two volumes over one file means
   whichever wrote last wins, silently.
5. **`security.refresh-period` is set.** Without it Trino reads the access-control rules
   once at startup and never again.
6. **`/etc/trino` is mounted file by file.** Kubernetes will not place a single file inside a
   read-only directory volume -- runc fails the container with `not a directory` and Trino
   never starts -- and Apchi mounts `resource-groups.properties` and
   `event-listener.properties` there itself. The official `trinodb/charts` chart mounts the
   whole directory, which is why Apchi does not support it (ADR-0008).

Both roles run with a writable `emptyDir` at `/data/trino`, which `node.properties` names as
`node.data-dir`: the launcher writes its pid file and an `etc` symlink there, and the image's
own copy belongs to uid 1000, so without it Trino cannot start as any other uid.

**Resource groups and event listeners are not mounted by the chart.** For both, the
*absence* of the mount is how "none configured" is expressed, since Trino refuses to start
when a file it was told to read is missing. Apchi adds and removes those two volumes itself,
and they are the only part of the Trino pod template it writes.

**TLS is not configured by the chart.** `trino.authentication.type: certificate` selects
which authenticator reads the user-mapping file; it does not set up a keystore. That goes
through `trino.additionalCoordinatorProperties` and mounts of your own, and then Apchi has to
be told: `trino.url=https://...`, and `trino.tls.*` so it has a certificate to present.
Apchi asserts this before every Apply and refuses rather than letting the queries time out.

**Autoscaling, ingress, exchange managers, JMX export, group providers** are not here. A
value this chart lacks is a gap to add.

## Not covered by this chart

**MongoDB, properly.** `mongodb.deploy` is for trying Apchi out: one replica, no
authentication, an emptyDir, and every Snapshot gone when the pod restarts. A real install
points `mongodb.uri` at a MongoDB the platform operates. Keeping it is the platform's job,
Apchi does not back it up, schedule
anything, or manage retention.

Apchi keeps one number outside it: `highWaterConfigMap`, the highest Snapshot it has ever
committed. Adoption reads it to tell a Cluster that was never managed from one whose database
is simply empty right now, because adopting the second would renumber it from Snapshot 1.
Apchi creates it itself, so it is not part of the release and `helm uninstall` leaves it
behind -- deliberately, because the next install into this namespace should find it.

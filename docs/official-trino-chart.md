# Running Apchi against the official Trino chart

`docs/getting-started.md` installs `charts/trino`, which ships with Apchi and mounts
`/etc/trino` file by file. Most clusters are not that. The official
[`trinodb/charts`](https://github.com/trinodb/charts) chart mounts its coordinator ConfigMap at
the whole of `/etc/trino`, and so does nearly every chart written by hand — it is the normal
idiom.

This page is the values an Admin sets to run Apchi against that shape. Verified against chart
`1.44.0`, Trino `483`.

## Why the shape matters

Kubernetes will not place a single file inside a read-only directory volume. The target file
does not exist in that volume, so there is nothing to bind onto:

```
error mounting ".../volume-subpaths/extra/c/1" to rootfs at "/etc/trino/user-mapping.json":
  not a directory
-> ContainerCannotRun
```

Trino logs nothing, because it never starts. A nested **directory** mount in the same position
works fine, which is what the configuration below relies on.

Apchi refuses such a deployment before an Apply rather than discovering it mid-Rollout — §16's
sixth precondition. Why it is handled this way rather than by declaring the chart unsupported:
[ADR-0007](adr/0007-the-deployment-owns-the-pinned-pointers.md).

## The values

Three pointers, five directories, one initContainer.

```yaml
# The three pointers. Static plumbing: an Operator never edits these, and they are the same on
# every cluster. Apchi fills the directories they name.
accessControl:
  type: properties
  properties: |
    access-control.name=file
    security.refresh-period=1s
    security.config-file=/etc/trino/access-control/rules.json

resourceGroups:
  type: properties
  properties: |
    resource-groups.configuration-manager=file
    resource-groups.config-file=/etc/trino/resource-groups/resource-groups.json

additionalConfigProperties:
  - event-listener.config-files=/etc/trino/event-listener/event-listener.properties
  - http-server.authentication.insecure.user-mapping.file=/etc/trino/user-mapping/user-mapping.json

# Apchi's five Secrets, each as a whole directory. No subPath anywhere: a subPath mount never
# receives updates, so Apchi would write a file and Trino would never see the change.
#
# Top level, not under `coordinator`. `additionalConfigProperties` above reaches the worker
# ConfigMap as well as the coordinator's, and Trino validates every path property on every
# node -- so a coordinator-only mount gives workers the properties without the files and they
# refuse to start with "file does not exist". Client certificates have to be on workers anyway
# (ADR-0005).
#
# Client certificates are deliberately NOT here. `secretMounts` renders only `name` and
# `secretName` into the volume, with no way to set `defaultMode`, so a private key mounted
# through it arrives world-readable -- see "Private keys need a mode" below.
secretMounts:
  - {name: apchi-access-control,  secretName: trino-access-control,  path: /etc/trino/access-control}
  - {name: apchi-user-mapping,    secretName: trino-user-mapping,    path: /etc/trino/user-mapping}
  - {name: apchi-resource-groups, secretName: trino-resource-groups, path: /etc/trino/resource-groups}
  - {name: apchi-event-listener,  secretName: trino-event-listener,  path: /etc/trino/event-listener}

# The group that owns every Secret volume's files. Without it the modes below are root-only
# and Trino cannot read its own keys. runAsUser and runAsGroup are the chart's own defaults,
# repeated because setting this key replaces the whole block.
securityContext:
  runAsUser: 1000
  runAsGroup: 1000
  fsGroup: 1000

coordinator:
  # Apchi keeps the durable catalogs in a Secret, because Trino's FileCatalogStore enumerates
  # its directory only in its constructor -- anything not on disk at pod start is absent until
  # the next one. The initContainer copies them into a writable directory before Trino reads it.
  additionalVolumes:
    - name: catalog-seed
      secret:
        secretName: trino-catalog-seed
        optional: true          # Apchi creates it; the first install must not wait for that
        defaultMode: 0640       # holds every catalog's connection-password
    - name: catalog-store
      emptyDir: {}
    - name: apchi-client-certificates
      secret:
        secretName: trino-client-certificates
        optional: true
        defaultMode: 0640       # private keys
  additionalVolumeMounts:
    - name: catalog-store
      mountPath: /data/trino/catalogs
    - name: apchi-client-certificates
      mountPath: /etc/trino/certs
      readOnly: true

worker:
  # Workers open their own connections to data sources, so a certificate only the
  # coordinator can read is a catalog that works for metadata and fails for data
  # (ADR-0005). Declared again here because these keys are per-node.
  additionalVolumes:
    - name: apchi-client-certificates
      secret:
        secretName: trino-client-certificates
        optional: true
        defaultMode: 0640
  additionalVolumeMounts:
    - name: apchi-client-certificates
      mountPath: /etc/trino/certs
      readOnly: true

initContainers:
  coordinator:
    - name: seed-catalogs
      image: "trinodb/trino:483"
      command: ["sh", "-c"]
      args:
        - mkdir -p /data/trino/catalogs &&
          cp -L /data/trino/catalog-seed/*.properties /data/trino/catalogs/ 2>/dev/null || true
      volumeMounts:
        - {name: catalog-seed, mountPath: /data/trino/catalog-seed, readOnly: true}
        - {name: catalog-store, mountPath: /data/trino/catalogs}
```

## Before you install: four Secrets must already have content

Trino validates every path property at startup and refuses to boot when a file one names is
missing. Three of these properties point into Apchi's directories, so those Secrets cannot be
empty when the coordinator first starts — and Apchi creates them on its first Apply, which is
after. Create them yourself first, or the install crashloops:

```
Invalid configuration property with prefix '': file does not exist:
  /etc/trino/event-listener/event-listener.properties
```

| Secret | Needs | Because |
|---|---|---|
| `trino-access-control` | `rules.json` | `security.config-file` names it |
| `trino-user-mapping` | `user-mapping.json` | `...user-mapping.file` names it |
| `trino-resource-groups` | `resource-groups.json` | `resource-groups.config-file` names it |
| `trino-event-listener` | `event-listener.properties` | `event-listener.config-files` names it |

`trino-client-certificates` and `trino-catalog-seed` may be absent or empty — nothing names a
file inside them.

The two Apchi normally leaves empty are the resource groups and event listener ones, because
in Apchi's default mode their *absence* is how "none configured" is expressed. On this chart
that is not available, which is the same limit as the one below arriving at install time
rather than later. `deploy/trino-dev/` is a working source of all four.

**The resource groups document needs a selector.** A document with groups and no selectors
starts cleanly and then rejects every query:

```
Query ... failed: No matching resource group found with the configured selection rules
```

That is also the reason Apchi will not render an empty document for you — see below.

## Private keys need a mode, and `secretMounts` cannot give them one

A Kubernetes Secret volume writes its files `0644` and owns them as **root** unless the pod
says otherwise. For a Client Certificate's private key that is two problems: every process
in the pod can read it, and a libpq-based client refuses to use it at all —

```
private key file "/etc/trino/certs/pg-demo.key" has group or world access; file must have
permissions u=rw (0600) or less if owned by the current user, or permissions u=rw,g=r
(0640) or less if owned by root. Current permissions: rw-r--r--
```

**`0600` is not the fix.** Trino runs as uid 1000 and a Secret file is owned by root, so
`0600` is a file Trino cannot read at all. Only the *group* follows `fsGroup`, which is why
both settings above are needed and neither works alone. Measured on a real cluster with a
readOnly mount:

| volume config | resulting mode | Trino can read | libpq accepts |
|---|---|---|---|
| neither | `0644 root:root` | yes | **no** |
| `fsGroup: 1000` alone | `0644 root:trino` | yes | **no** |
| `defaultMode: 0640` + `fsGroup: 1000` | `0640 root:trino` | yes | yes |

Asking for `0600` is harmless but does nothing: the kubelet ORs `roMask = 0440` into every
file's mode on a readOnly volume, so `0600` arrives as `0640` anyway. `0640` is the floor,
and it is exactly what libpq permits for a root-owned file.

**This is why the certificates are not in `secretMounts`.** That key renders only `name` and
`secretName` into the volume —

```yaml
{{- range .Values.secretMounts }}
- name: {{ .name }}
  secret:
    secretName: {{ tpl .secretName $ }}
{{- end }}
```

— with nowhere to put `defaultMode`. `additionalVolumes` takes a volume spec verbatim, so
that is where a credential-bearing Secret goes. The cost is declaring it twice, once for the
coordinator and once for the workers, because those keys are per-node.

The other four Apchi Secrets stay in `secretMounts`: the access-control rules, the user
mapping and the resource-group rules carry no credentials. An Event Listener's properties
can — a JDBC URL with a password in it — so move that one the same way if you configure one
with embedded credentials.

Apchi's own `charts/trino` sets both halves already, and `scripts/check_trino_chart.py`
fails if either is dropped.

## Why `type: properties`

Note `accessControl.type: properties` and `resourceGroups.type: properties` rather than
`configmap`. With `configmap` the chart mounts its *own* volume at `/etc/trino/access-control`
and `/etc/trino/resource-groups`, which would collide with Apchi's Secret at the same path.
`properties` writes the pointer and creates no volume, leaving the directory to Apchi.

## And in Apchi's own values

Two of those pointers are ones Apchi would otherwise provide itself, so Apchi has to be told
the deployment owns them:

```yaml
deployment:
  providesResourceGroupsProperties: true
  setsEventListenerConfigFiles: true
```

Without these Apchi tries to mount `resource-groups.properties` and
`event-listener.properties` as single files, and the precondition above refuses the Apply.
With them set but the pointers missing, a different precondition refuses it — a Section
writing files nobody reads looks applied and does nothing, which is worse.

## What this costs

**Event Listeners cannot be emptied.** The pointer always names a file and Trino refuses to
start when a file it was told to read is missing, so removing the last listener would leave a
coordinator that cannot come back. Trino ships no listener that does nothing — `http`, `mysql`
and `kafka` each need a real sink — so there is nothing to render in its place and Apchi
refuses the delete, naming the pointer to remove first.

**It must be populated before any Apply succeeds**, including an Apply that only adds a
catalog. Apchi writes each Secret whole, so an Apply with that Section empty would write an
empty Secret over the file the pointer names and the next restart would not come back. Since
Trino cannot start without that file in the first place, a running cluster here always has a
listener — adopt it into the Candidate (§15) rather than leaving it unmodelled.

**Resource Groups can be emptied, and behaves exactly as it does elsewhere.** Trino supplies
the answer here: when `etc/resource-groups.properties` is absent,
`InternalResourceGroupManager` returns early and `LegacyResourceGroupConfigurationManager`
stays in force — matching every query into a group called `global` at
`query.max-concurrent-queries` (default 1000) and `query.max-queued-queries` (default 5000).

So an empty Section renders precisely that, catch-all selector included:

```json
{"rootGroups": [{"name": "global", "hardConcurrencyLimit": 1000, "maxQueued": 5000}],
 "selectors": [{"group": "global"}]}
```

The selector is not decoration. The legacy manager matches queries itself rather than
consulting selectors, and a document with groups and none rejects every query with
`No matching resource group found with the configured selection rules`.

Worth knowing either way: **"no resource groups" has never meant "no limits" in Trino.** A
cluster with no rules file still runs under 1000 concurrent and 5000 queued, so emptying this
Section returns you to those numbers rather than removing all governance.

Apchi does not read your `query.max-concurrent-queries` / `query.max-queued-queries` when
rendering that fallback — both are deprecated and rarely set, and being wrong costs a
concurrency ceiling rather than an outage. Set the limits you want as a real resource group if
you have tuned them.

On `charts/trino` the Event Listeners limit does not apply, because Apchi owns that mount
there.

**Admin raw files do not work** (§14, [#161](https://github.com/InfinityHub123/apchi/issues/161)).
Their path is the Admin's to choose, which makes them single-file mounts by nature, so every
path under `/etc/trino` is refused — `config.properties` included, since that file belongs to
the chart's own ConfigMap. Use the chart's `additionalConfigProperties` instead; it is the same
capability by a different route.

## What was actually verified

Everything above was run, not reasoned about. On minikube, chart `1.44.0`, Trino `483`:

```
trino-coordinator-699cdb745d-474sh   1/1   Running
trino-worker-58b5f866f-npfnn         1/1   Running

trino> SELECT node_id, state FROM system.runtime.nodes
"trino-coordinator-699cdb745d-474sh","active"
"trino-worker-58b5f866f-npfnn","active"
```

with all five directories present and all three pointers resolving:

```
/etc/trino/access-control    rules.json
/etc/trino/certs             (empty, which is correct)
/etc/trino/user-mapping      user-mapping.json
/etc/trino/resource-groups   resource-groups.json
/etc/trino/event-listener    event-listener.properties
```

Both costs were reproduced as well: an empty `trino-event-listener` Secret stopped the
coordinator booting, and a resource groups document without selectors rejected every query.
Neither is a guess.

## Checking it before you install

Apchi's own preconditions can be run against a rendered chart, which is cheaper than finding
out during an Apply:

```python
import yaml
from app.config import Settings
from app.pipeline import preconditions

docs = [d for d in yaml.safe_load_all(open("rendered.yaml")) if d]
spec = next(
    d["spec"]["template"]["spec"]
    for d in docs
    if d.get("kind") == "Deployment" and "coordinator" in d["metadata"]["name"]
)

await preconditions.check(kubernetes, preconditions.pod_spec(spec), settings)
```

`scripts/check_trino_chart.py` does exactly this for `charts/trino` in CI, and is the closest
working example.

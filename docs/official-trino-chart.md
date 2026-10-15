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
fifth precondition.

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

coordinator:
  # Apchi's five Secrets, each as a whole directory. No subPath anywhere: a subPath mount
  # never receives updates, so Apchi would write a file and Trino would never see the change.
  secretMounts:
    - {name: apchi-access-control,      secretName: trino-access-control,      path: /etc/trino/access-control}
    - {name: apchi-client-certificates, secretName: trino-client-certificates, path: /etc/trino/certs}
    - {name: apchi-user-mapping,        secretName: trino-user-mapping,        path: /etc/trino/user-mapping}
    - {name: apchi-resource-groups,     secretName: trino-resource-groups,     path: /etc/trino/resource-groups}
    - {name: apchi-event-listener,      secretName: trino-event-listener,      path: /etc/trino/event-listener}

  # Apchi keeps the durable catalogs in a Secret, because Trino's FileCatalogStore enumerates
  # its directory only in its constructor -- anything not on disk at pod start is absent until
  # the next one. The initContainer copies them into a writable directory before Trino reads it.
  additionalVolumes:
    - name: catalog-seed
      secret:
        secretName: trino-catalog-seed
        optional: true          # Apchi creates it; the first install must not wait for that
    - name: catalog-store
      emptyDir: {}
  additionalVolumeMounts:
    - name: catalog-store
      mountPath: /data/trino/catalogs

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

**Resource Groups and Event Listeners can no longer be emptied.** Each pointer always names a
file, and Trino refuses to start when a file it was told to read is missing — so removing the
last resource group or the last listener would leave a coordinator that cannot come back.
Apchi refuses the delete and names the pointer to remove first.

Nothing harmless is rendered instead, and the reason differs. An empty resource groups
document makes Trino reject every query matching no selector, so a no-op would need a
catch-all root group — and `hardConcurrencyLimit` and `maxQueued` are required integers with
no "unlimited", so Apchi would be choosing a concurrency policy for you. Event Listeners have
no no-op at all: `http`, `mysql` and `kafka` each need a real sink.

On `charts/trino` neither limit applies, because Apchi owns those mounts there.

**Admin raw files do not work** (§14, [#161](https://github.com/InfinityHub123/apchi/issues/161)).
Their path is the Admin's to choose, which makes them single-file mounts by nature, so every
path under `/etc/trino` is refused — `config.properties` included, since that file belongs to
the chart's own ConfigMap. Use the chart's `additionalConfigProperties` instead; it is the same
capability by a different route.

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

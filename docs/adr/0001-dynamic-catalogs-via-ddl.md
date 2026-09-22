# Catalogs are applied via SQL DDL, not configuration files

Every other Section Apchi manages is applied by writing a file — permissions and client
certificates are then picked up by Trino on its own, while certificate mapping, resource
groups and event listeners need a restart.

Catalogs are the exception: Apchi issues `CREATE CATALOG` / `DROP CATALOG` against the running
coordinator, with `catalog.management=dynamic` and `catalog.store=file`. Catalog changes
therefore need no restart and destroy no running queries.

Durability lives in **etcd, not in a volume**. A Secret holds the catalog `.properties`; an
initContainer copies it into `catalog.config-dir` — a plain path on the container filesystem —
before Trino starts. Trino writes there at runtime; the copy is thrown away with the pod and
rebuilt from the Secret at the next start. No PersistentVolume is involved.

## Considered options

**Catalog files in a ConfigMap, restart to apply.** Matches how every other Section is
delivered and needs no new privileges, but a coordinator restart per catalog change — and
catalogs are the most frequently changed Section.

**`catalog.store=memory`.** Avoids a writable directory entirely. Rejected:
`InMemoryCatalogStore` holds catalogs in a `ConcurrentHashMap` and the docs state that with
`memory`, "any existing files are ignored on startup". Every coordinator restart would bring
the Cluster up with zero catalogs until Apchi re-issued every `CREATE CATALOG` — making
Trino's availability depend on Apchi being alive, and requiring Apchi to detect the drift with
a reconciliation loop this design does not have. It would also entangle the Apply engines: a
rollout for an unrelated Section would wipe every catalog.

**The Secret mounted directly at `catalog.config-dir`.** The most economical design — one
volume, no copy step, durability and delivery in the same object. **Not possible: Secret
volume mounts are read-only, and no permission setting changes that.** Verified empirically on
Kubernetes v1.35.1, running as root with `defaultMode: 0777` and `readOnly: false` both set:

```
tmpfs on /x type tmpfs (ro,seclabel,relatime,...)
touch /x/new.properties   → Read-only file system
echo >> /x/a.properties   → Read-only file system
```

`defaultMode` sets permission bits on the projected symlinks; `ro` is a mount flag, and the
kernel refuses the write before permissions are consulted. In source, `secret.go`'s
`GetAttributes()` returns `ReadOnly: true` unconditionally and `kubelet_pods.go`'s
`makeMounts()` computes `mount.ReadOnly || mustMountRO`, so `readOnly: false` is silently
overridden. This has been unconditional since Kubernetes v1.10, when kubernetes#58720 made
these volume types read-only to fix CVE-2017-1002102; the `ReadOnlyAPIDataVolumes` gate that
allowed opting out is gone.

**`catalog.store=file` on a PersistentVolume.** Works, and needs no initContainer, because the
directory simply persists. Rejected in favour of the seed: a PVC means storage provisioning,
`fsGroup`, and a stateful volume attached to a component that holds no state of its own.

**`catalog.store=file` on the container filesystem, seeded from a Secret (selected).** Trino
writes to a plain directory it owns. The Secret is the durable record. An initContainer bridges
them at pod start, so the Cluster comes up with its full catalog set without Apchi needing to
be reachable.

Note the contrast with [ADR-0005](./0005-single-certificate-secret.md): client certificates are
written by **Apchi** and only read by Trino, so a read-only mounted Secret is correct there —
and its live updates are what make certificates apply without a restart. The writer decides the
delivery: a read-only projected volume wherever Apchi writes, a writable directory wherever
Trino does.

## Consequences

- `catalog.config-dir` must be declared in `etc/catalog-store.properties`, not
  `config.properties` — the filename is hardcoded in `CatalogStoreManager.java` and setting it
  in `config.properties` fails validation.
- **The deployment needs an initContainer.** Without it the Cluster starts with no catalogs.
  It is a precondition Apchi checks before every Apply (§16), not an assumption.
- **Apply writes twice: the Secret, then the DDL.** The two can diverge, and the ordering is
  what makes divergence recoverable — see §10.
- Catalog DDL is restricted to Apchi's identity through the generated access-control rules.
  Without it, anyone with sufficient Trino privileges can `DROP CATALOG`, removing it from the
  store but not from the Secret, so it returns at the next restart. `catalog.read-only=true`
  would prevent the drop but also blocks Apchi's own DDL.
- **Apply is no longer atomic for this Section.** See ADR-0004.
- "No restart" is not "instant". A catalog referencing a client certificate cannot connect
  until that certificate's Secret has propagated, so Apply writes the Secret first and retries
  the DDL until it succeeds.
- Credentials inlined in `CREATE CATALOG` are delivered unredacted to any configured event
  listener. Accepted; see the Catalogs section of the implementation document.
- Trino's docs mark dynamic catalog management experimental: "the syntax might change and be
  backward incompatible." Accepted.

# Trino deployment for development and Tier 2 tests

A Trino satisfying §7.1 of `apchi_implementation.md`, small enough to run on kind or
minikube. It is the fixture the end-to-end tests need, and an executable reference for what
the production chart must do.

```sh
kubectl apply -f deploy/trino-dev/
kubectl wait --for=condition=ready pod -l app=trino --timeout=300s
```

`05-access-control.yaml` is **generated** — `scripts/export_access_control.py` writes it from
Apchi's own generator, and CI fails if the two disagree. Do not edit it by hand. It is committed
only because Trino refuses to boot without the file, so the coordinator has to be able to start
before Apchi has ever run; Apchi rewrites the Secret on every Apply.

## The mechanism

```
Secret trino-catalog-seed ──read-only──▶ /data/trino/catalog-seed
   ▲ patched by Apchi                              │ initContainer copies at pod start
   │                                               ▼
   └── durability lives here          /data/trino/catalogs  ◀── Trino writes (CREATE CATALOG)
```

The store directory is an `emptyDir`, thrown away with the pod and reseeded from the Secret
at every start. There is no PersistentVolume: durability lives in etcd.

## What testing this on a real cluster corrected

Four things that are not obvious from the documentation, each of which fails at runtime
rather than at review:

**`catalog.store` and `catalog.config-dir` go in different files.** `catalog.store=file`
belongs in `config.properties`; putting it in `catalog-store.properties` is rejected with
_"Configuration property 'catalog.store' was not used"_. `catalog-store.properties` accepts
only `catalog.config-dir` and `catalog.read-only`.

**`etc/catalog-store.properties` resolves to `/etc/trino/catalog-store.properties`.**
`CatalogStoreManager` hardcodes that relative path. The image's `WorkingDir` metadata says
`/`, but the launcher runs from `/data/trino`, whose `etc` is a symlink to `/etc/trino`.
Reading the image metadata gives the wrong answer.

**The store directory must be writable by the non-root `trino` user.** A path under `/var`
fails with `mkdir: Permission denied` before Trino starts. `/data/trino` is trino-owned, so
`catalog.config-dir` lives there and no `fsGroup` is needed.

**`access-control.name` belongs only in `access-control.properties`.** In
`config.properties` it is rejected with _"Did you mean to use 'access-control.config-files'?"_

## What it proves

Verified on Kubernetes v1.35.1 with Trino 483:

- The seeded catalog is loaded at startup, so the Cluster comes up with its full catalog set
  without Apchi being reachable.
- Apchi's identity can `CREATE CATALOG`; another identity gets
  `Access Denied: Cannot create catalog`.
- That other identity still has full access to existing catalogs and can query them — the
  catch-all rule doing its job. Without it, every user is denied every catalog.
- **A catalog created by DDL but never written to the Secret disappears at the next pod
  restart**, while the seeded one survives. This is §10's second divergence case, and it is
  why Apchi patches the Secret before issuing the DDL.

## Not production

One replica each, no TLS, no authentication, `tpch` as the seeded catalog, and Apchi's
identity is the literal string `apchi`. The production chart must satisfy the same §7.1
requirements; this is not that chart.

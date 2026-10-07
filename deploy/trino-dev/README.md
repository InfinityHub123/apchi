# Trino deployment for the end-to-end tests

The same Trino `charts/trino` installs, as raw manifests. It exists because the tier 2
suite needs a fixture it can apply and reset in seconds without a Helm release in the way,
and because `tests/tier2/conftest.py` restores the Cluster to these exact manifests between
tests (#53).

**For installing Trino, use `charts/trino`.** It is the same deployment with values, a
bootstrap that survives `helm upgrade`, and a CI gate that runs Apchi's own preconditions
against what it renders.

The two are the same where it matters — the coordinator's mounts and volumes are identical,
which is the part Apchi's preconditions are about — but they are kept in step **by hand**,
which is the kind of duplication this repository otherwise refuses. Generating these
manifests from the chart, the way `openapi.json` and the two Secrets below are generated, is
#95.

```sh
kubectl apply -f deploy/trino-dev/
kubectl wait --for=condition=ready pod -l app=trino --timeout=300s
```

`05-access-control.yaml` and `06-user-mapping.yaml` are **generated** —
`scripts/export_access_control.py` and `scripts/export_user_mapping.py` write them from Apchi's
own generators, and CI fails if the two disagree. Do not edit them by hand. They are committed
only because Trino refuses to boot without either file, so the coordinator has to be able to
start before Apchi has ever run; Apchi rewrites both Secrets as the configuration changes. The
same generators write `charts/trino/files/`, so there is one source and no second copy to drift.

The coordinator sets `http-server.authentication.insecure.user-mapping.file` rather than the
certificate variant, because there is no TLS here and Trino rejects
`http-server.authentication.certificate.user-mapping.file` unless certificate authentication is
configured. Both properties feed the same file to the same parser, so a pattern proven here is
a pattern proven for production. Two consequences worth knowing before you run a query against
this cluster: the rules are read once, when the authenticator is built, so a change takes only
on a Rollout; and a principal no rule matches is **denied**, not passed through — which is why
Apchi always emits the catch-all `(.*)` rule when no Certificate Mapping Pattern is set.

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

A fifth, found while writing `charts/trino`: **each configuration file is mounted
separately, deliberately.** One mount of the whole `/etc/trino` directory would be more
idiomatic and would break the cluster — Apchi mounts a single file inside that directory,
and a `subPath` mount at a path inside a ConfigMap directory mount fails the container
outright, with no Trino log at all. See #93.

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
identity is the literal string `apchi`. `charts/trino` is the one to install; this is the
test fixture.

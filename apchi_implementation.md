# Apchi Implementation Document

Terminology in this document follows [CONTEXT.md](./CONTEXT.md). Decisions with lasting
consequences are recorded in [docs/adr/](./docs/adr/).

---

# 1. Purpose

Apchi is the control plane for configuring Trino. It exposes two supported interfaces:

- Web UI
- Versioned REST API

Every capability available in the UI is also available through the REST API. Nobody should
need to automate against or scrape the UI. The REST API is documented with OpenAPI.

Apchi lets Operators work with high-level Trino concepts rather than editing authorization
rules, catalog property files, certificate mappings or resource-group files by hand.

# 2. Scope and deployment model

**One Apchi deployment manages exactly one Trino Cluster.** Snapshot numbering is a single
sequence within that Cluster. Apchi never writes to two Clusters — doing so would require
solving partial-apply atomicity across clusters, a strictly harder problem than the one
this design solves, and would put both clusters behind one failure path.

Apchi runs on Kubernetes and reaches Trino through a Kubernetes service account. It edits
ConfigMaps and Secrets, triggers Deployment rollouts, and issues SQL to the coordinator.

## Managed areas

Each is a **Section** of the configuration:

- Catalogs
- Client Certificates
- Certificate Mapping
- Permissions
- Resource Groups
- Event Listeners

# 3. Roles

**Operators** are the platform staff at the organisation Apchi is operated for. They are the
primary Apchi users and own all six Sections.

**Admins** provide and operate the underlying platform. In addition to everything Operators
can do, they can inject arbitrary low-level Trino configuration values and can freeze
Operator mutation (§14).

**End Users** issue queries to Trino. They are never Apchi users — only the subject of
permissions and resource groups.

# 4. The configuration model

Three concepts, and only three.

**Snapshot.** An immutable, complete record of the Apchi-managed desired configuration that
has passed validation, been applied to the Cluster, and been verified against it. Numbered
sequentially. A Snapshot is known-good by construction.

**Configuration Candidate.** The single mutable configuration Operators edit, derived from
the latest Snapshot. There is exactly one per Cluster. Every Operator change lands in it.
Nothing reaches the Cluster until Apply.

**Effective Cluster State.** What the Cluster is actually running. Changed only by Apply.

There is no transaction object — see [ADR-0002](./docs/adr/0002-configuration-candidate.md).
An Operator does not create or unlock anything; they simply edit, and their changes
accumulate in the Candidate:

```
POST  /api/v1/catalogs
PUT   /api/v1/resource-groups/batch
GET   /api/v1/review          → diff between Candidate and latest Snapshot
POST  /api/v1/applies
```

## Concurrency

The Candidate is shared. Two Operators editing at the same time edit the same object, and
an Apply promotes everything in it — including changes the caller did not make.

This is mitigated, not eliminated:

- `GET /review` returns the full diff between the Candidate and the latest Snapshot. The UI
  calls it before every Apply and displays every Section, not only the caller's changes.
- The audit trail records who changed what.

An API caller who skips `review` can apply another Operator's staged work. Accepted, on the
basis that one Operator interacts with the API at a time.

A sharper consequence of sharing: **an invalid resource blocks everyone's Apply** until it is
fixed or reverted. With per-Operator transactions a bad payload would be its author's problem;
here it is the Cluster's. That is why static validation rejects at request time rather than
storing the resource and complaining later (§6).

**The Candidate is frozen during Apply.** Mutations are rejected with `409` for the whole of
an Apply — every engine, not only the one that restarts Trino — so what is committed is what
was verified.

## Candidate lifecycle

- **Apply succeeds** — the Candidate is re-derived from the new Snapshot; its diff is empty.
- **Apply fails** — the Candidate is preserved exactly as-is, including the changes that
  failed, so the Operator can fix and retry.
- **Reset** — an explicit endpoint discards all changes and re-derives the Candidate from
  the latest Snapshot. Because nothing reaches the Cluster before Apply, Reset leaves the
  Effective Cluster State untouched.

# 5. The lifecycle

```
EDIT → VALIDATE → APPLY → VERIFY → COMMIT
```

These stages stay separate internally even where the UI presents them as one action. A
Snapshot is created only after all of them succeed.

# 6. Validation

Validation answers: *should this configuration be applied to the Cluster?* It runs before
anything is touched, so a validation failure leaves the Cluster completely unchanged.

Validation runs at two moments, and the distinction matters:

| | When | Cost |
|---|---|---|
| **Static validation** | every request | none — Apchi's own data, no cluster |
| **Trino validation** | once per Apply, before the Cluster is touched | one ephemeral pod |

## Static validation

Runs on **every mutating request**, so a bad payload is rejected before it ever enters the
Candidate (§19 maps failures to status codes). Covers: schema, types, required fields,
unknown fields, malformed configuration, invalid references, impossible values,
permission-rule structure, resource-group structure, certificate structure, and catalog
properties wherever Apchi has a schema for the connector (§13.1).

Rejecting at request time matters more here than in most designs, because the Candidate is
shared: a payload that gets a `201` becomes every Operator's problem until someone fixes or
reverts it (§4).

### How deeply a Section can be typed depends on who owns its model

| Section | Model owned by | Typing |
|---|---|---|
| Permissions | Apchi | fully typed, no escape hatch |
| Certificate Mapping | Apchi | fully typed, no escape hatch |
| Resource Groups | Trino, but a fixed documented schema | fully typed, no escape hatch |
| Catalogs | Trino **plugins** | curated per connector, pass-through for the rest (§13.1) |
| Event Listeners | Trino **plugins** | curated per listener type, pass-through for the rest (§13.6) |
| Client Certificates | — | not a schema problem: parse the archive, match cert to key, read CN and expiry |
| Admin arbitrary config | nobody | untyped by design; only the ephemeral pod can validate it (§14) |

Four of the six Sections are fully typed with no escape hatch, because Apchi generates the
Trino format from its own model and there is nothing to pass through. Only catalogs and event
listeners need curation, because only those two have their properties defined by whichever
plugin happens to be loaded.

### Within-resource at request time, cross-resource at Validate

A `POST` checks the resource in front of it. **References between resources are checked at
Validate, over the whole Candidate** — a permission naming a catalog, a catalog naming a
client certificate.

Enforcing references at request time would make **order of entry matter**: adding a permission
before the catalog it names would fail, even though the Candidate is perfectly coherent once
both exist. That is a poor constraint to impose on an Operator filling in a screen. So `422`
means "your payload is wrong" and Validate means "your Candidate is incoherent".

## Trino validation

Static checks cannot tell you whether Trino will accept a configuration. Apchi brings up an
**ephemeral coordinator-only pod** — same Trino image and version as the Cluster — and checks
that it starts.

This runs **once per Apply, against the whole Candidate**, not once per resource. A pod per
request would be slow and expensive for something the Validate action (§9) already offers on
demand.

No workers are needed; this tests whether configuration *loads*, not whether queries run.
The signal is `/v1/info` reporting `"starting": false`, the same check the standard
readiness probe uses.

Two things about the pod come from the Trino image rather than from Apchi, and both are easy to
get wrong:

- Dynamic catalogs are switched on through the `CATALOG_MANAGEMENT` environment variable, which
  the image's own `config.properties` reads. Apchi therefore mounts no config file and overrides
  no command.
- The image ships **example catalogs** — `jmx`, `memory`, `tpch`, `tpcds` — in
  `/etc/trino/catalog`. The validation pod hides them behind an empty volume, so the probe holds
  exactly what the Candidate declares. Without that, a Catalog an Operator quite reasonably named
  `memory` fails Validation as already existing, for a reason having nothing to do with it. The
  Cluster needs no such mount: pointing `catalog.config-dir` elsewhere already stops Trino
  reading that directory.

**Validation has two modes**, because Apply does:

- **File-based Sections** (permissions, resource groups, event listeners, certificate
  mapping) — mount the Candidate's generated ConfigMap and confirm the pod reaches ready.
- **Catalogs** — bring the pod up, then issue the `CREATE CATALOG` statements against it and
  confirm they succeed. When a catalog under test references a client certificate, the
  validation pod must mount the certificate Secret too, or it fails for the wrong reason.

Requirements:

- A **hard timeout**. A pod that never becomes ready fails Validation; it must not hang the
  pipeline.
- **Network reachability**, in both directions. The validation pod must reach the data sources a
  catalog references, or catalog validation fails for reasons unrelated to the configuration.
  Apchi must reach the pod: it connects to the pod's own address on 8080, which assumes Apchi
  runs in the Cluster and that no default-deny NetworkPolicy stands between them. In a
  namespace with default-deny ingress, Validation needs a policy allowing Apchi to reach pods
  labelled as validation pods, or every Validation fails as a coordinator that never served.
- **Cleanup on Apchi crash**, or orphaned pods accumulate.

### The checks that need no pod run first

The ephemeral coordinator starts **empty**, which means it cannot see a collision with a
catalog that exists on the Cluster but is not managed by Apchi — one seeded before Apchi
arrived, say. That check is therefore made against the Cluster before the pod is created: a
Catalog the Candidate would create must not already exist there. Without it the DDL fails
*after* Apply has written the Secret, which is the divergence of §10 for a reason the Operator
could have been told about before anything moved. Adoption (§15) is how such a catalog comes
under management.

A Candidate with no catalogs gets no pod at all. The pod is the expensive part of Validation,
and an empty Candidate is a real case: the first Apply of a fresh Cluster, and every Apply
that only drops things.

Note the limit honestly: some connectors initialise lazily, so a wrong JDBC URL can pass
startup validation and only fail at query time.

# 7. Apply

Apply makes the Effective Cluster State match the Candidate. It is the first and only point
at which Operator changes reach the Cluster.

There are **three Apply engines**, distinguished by how a change reaches Trino:

| Engine | Sections |
|---|---|
| DDL (§7.1) | Catalogs |
| File, no restart (§7.2) | Permissions, Client Certificates |
| File, with rollout (§7.3) | Certificate Mapping, Resource Groups, Event Listeners |

Only the third restarts Trino. §7.4 says which nodes.

## 7.1 DDL apply — catalogs only

Apchi issues `CREATE CATALOG` / `DROP CATALOG` against the running coordinator. No restart,
no query loss. See [ADR-0001](./docs/adr/0001-dynamic-catalogs-via-ddl.md).

**Whoever writes a directory decides how it is delivered.** Apchi writes five of the six
Sections, so those are mounted from a Secret or ConfigMap and the kubelet keeps them current —
read-only is exactly what is wanted there. Trino writes the catalog directory, and a Secret
mount is read-only, so that one directory alone is delivered differently:

| Directory | Written by | Delivery |
|---|---|---|
| Catalog `.properties` | **Trino** | Secret at a seed path → initContainer copy → writable container filesystem |
| `rules.json`, certificates, resource groups, event listeners, user mapping | Apchi | Secret or ConfigMap mounted directly; the kubelet keeps it current |

```
                    Apchi patches
                         │
                         ▼
Secret ──read-only──▶ /etc/trino/catalog-seed
                              │  initContainer copies at pod start
                              ▼
                    catalog.config-dir   ◀── Trino writes (container filesystem)
```

Requirements on the Trino deployment:

- `catalog.management=dynamic` and `catalog.store=file` in **`config.properties`**
- `catalog.config-dir` in **`etc/catalog-store.properties`** — a different file. That filename
  is hardcoded in `CatalogStoreManager.java`, and the relative path resolves to
  `/etc/trino/catalog-store.properties` (the launcher runs from `/data/trino`, whose `etc`
  symlinks to `/etc/trino`; the image's `WorkingDir` metadata is misleading). The two
  properties are **not interchangeable between the files**: `catalog.store` in
  `catalog-store.properties` is rejected as unused, and `catalog.config-dir` in
  `config.properties` likewise.
- `catalog.config-dir` pointing at a **plain path the non-root `trino` user can write** —
  under `/data/trino`, which is trino-owned. A path under `/var` fails with a permission error
  before Trino starts. No PVC and no `fsGroup` are needed.
- `access-control.name` in **`access-control.properties`** only; in `config.properties` it is
  rejected.
- A Secret of catalog `.properties` mounted read-only at a seed path, and an initContainer
  copying it into `catalog.config-dir` before Trino starts
- Catalog DDL restricted to Apchi's identity in the generated access-control rules

`FileBasedSystemAccessControl.checkCanCreateCatalog` and `checkCanDropCatalog` gate on the
**`owner`** access mode, so the restriction is a `catalogs` rules block:

```json
{"catalogs": [
  {"user": "<apchi identity>", "allow": "owner"},
  {"allow": "all"}
]}
```

First match wins. Apchi gets `owner` and can create and drop; everyone else gets `all`, which
is full access to existing catalogs but not `owner`. **The catch-all rule is mandatory** —
`canAccessCatalog` returns false when nothing matches, so omitting it denies every End User
access to every catalog, the same trap as the `queries` block in §13.4. Note Trino's docs list
only `all`, `read-only` and `none`; `owner` exists in the source (`AccessMode`, where `owner`
implies `all` implies `read-only`) and is what this depends on.

Durability lives in **etcd**, not in a volume. The Secret is the record that survives the pod;
the store directory is the live working copy, rebuilt from the Secret at every start. That is
why Trino needs no persistent storage and why the Cluster comes up with its full catalog set
whether or not Apchi is running.

**Apply therefore writes twice** — the Secret for durability, the DDL for liveness. Ordering
and failure handling: §7.5 and §10.

## 7.2 File apply, no restart — permissions and client certificates

Apchi writes the file and Trino picks it up on its own. Permissions are re-read on Trino's
`security.refresh-period` timer; a client certificate is simply read when a connection is
opened. Neither needs the pod restarted, so neither destroys a running query.

What this costs instead is that the change is **not immediate** — see §7.5.

## 7.3 File apply, with rollout — certificate mapping, resource groups, event listeners

Apchi writes the generated configuration to a ConfigMap or Secret and triggers a Deployment
rollout by patching `spec.template.metadata.annotations` — which is all `kubectl rollout
restart` does, so it is replicable through the Kubernetes API with no `kubectl` dependency.

**This terminates every running and queued query.** Trino cannot drain a coordinator:
`NodeStateManager.transitionState()` throws `UnsupportedOperationException("Cannot drain
coordinator")`, and graceful shutdown is documented as usable "exclusively on workers".
There is no coordinator HA. No configuration of Apchi changes this. See
[ADR-0003](./docs/adr/0003-coordinator-rollout.md).

The Apply confirmation must say so, in those words, with a live running-query count.

## 7.4 Minimal restart set

Apchi restarts only what the Candidate's changes require:

| Section | Coordinator | Workers |
|---|---|---|
| Catalogs | no restart (DDL) | no restart (DDL) |
| Client Certificates | no restart (Secret) | no restart (Secret) |
| Permissions | no restart (timed reload) | no |
| Certificate Mapping | restart | no |
| Resource Groups | restart | no |
| Event Listeners | restart | treat as required |

Resource groups: "The JSON file only needs to be present on the coordinator." Access
control: "Access control must be configured on the coordinator." Event listener scope is
genuinely unresolved in Trino's documentation — restart both.

This saves rollout time, not queries. The coordinator restart is what kills queries.

**A Candidate touching only catalogs, client certificates and permissions applies with no
restart at all.** Half the Sections reach the Cluster without one.

## 7.5 Timing

A file written by Apchi does not reach Trino instantly. Two delays stack:

1. **Kubelet projection.** Kubernetes propagates a ConfigMap or Secret change into the
   mounted volume. The kubelet's `configMapAndSecretChangeDetectionStrategy` defaults to
   `Watch`, so this is normally **a few seconds** — but `syncFrequency` (default 1 minute)
   is the worst case, and that is the bound to design against.
2. **Trino's own reload.** For permissions, the `security.refresh-period` timer, and the
   re-read is lazy: it happens on the next authorization check after expiry.

**Do not sleep a fixed duration.** A fixed 65-second wait is both usually wrong and
occasionally too short. Poll for the change to become observable, with a timeout:

- **Permissions** — run a probe query as the reserved identity (§8) that the Candidate's
  rules should newly allow or deny, until the answer matches.
- **Client certificates** — retry the catalog DDL that references the certificate, with
  backoff, until it succeeds or the timeout expires. The certificate must be on disk before
  a catalog using it can connect (§13.2), and Apchi cannot see the pod's filesystem to check
  directly.

**Catalogs: Secret first, then DDL.** The catalog Secret is patched before the DDL is issued.
A failed DDL then leaves a catalog recorded but not yet live — Apchi knows, because the DDL
returned an error, and can retry. The reverse order leaves a catalog that works now and
silently disappears at the next pod restart, possibly weeks later, with nothing connecting the
two events. There is no propagation race here: nothing reads the seed mount until the next pod
start, so the Secret write needs no wait before the DDL.

The Secret's contents are **replaced**, not merged: Apchi renders the whole catalog Secret from
the Candidate on every Apply, so a Catalog the Operator removed disappears from the durable copy
too. A Kubernetes merge patch merges the data map key by key, so removing a key takes an
explicit null — without it a dropped Catalog is seeded straight back in at the next pod restart,
which is the same silent, weeks-later failure in reverse. A consequence worth stating: a catalog
that exists on the Cluster but not in the Candidate is removed from the seed by the next Apply.
Bringing pre-existing catalogs under management is Adoption's job (§12), not Apply's.

# 8. Verification

Verification answers a different question from Validation: *did the Cluster adopt the
configuration and stay healthy?*

1. `/v1/info` — coordinator responding, `"starting": false`
2. `/v1/status` — node liveness
3. `SELECT count(*) FROM system.runtime.nodes WHERE NOT coordinator AND state = 'active'` —
   worker count matches the Kubernetes replica count
4. `SHOW CATALOGS` — every catalog in the Candidate is present
5. A smoke query against a default catalog, run as Apchi's reserved identity

Step 4 catches divergence between the catalog Secret and Trino's store while the Apply is
still in flight, rather than leaving it to surface at the next restart. It is Verification,
not a background reconciler, so §17 still holds.

Step 3 matters: the standard readiness probe only proves the JVM booted. **A coordinator
with zero workers passes it.** Use the `system.runtime.nodes` system table, not `/v1/node` —
that endpoint returns 404 on Trino 483, verified against a running cluster. The system table
is a documented SQL interface reachable over the connection Apchi already holds, and needs no
management credentials.

Step 4 is what distinguishes "the coordinator came back up" from "the coordinator came back
up running the configuration we just applied". Trino exposes no endpoint reporting which
version of a rules file is active, so verification must be functional rather than
introspective.

## The reserved identity

Apchi holds a reserved Trino Identity with `MANAGEMENT_READ` and `SELECT` on a designated
verification catalog. Its rule is injected at configuration-generation time, shown in the
Permissions UI as a system-owned row that Operators can see but not edit, and Validation
rejects any Candidate whose rules would shadow it.

Without this, the first Operator who tightens their permission matrix breaks Verification
for every future Apply with no indication why.

# 9. Commit

A Snapshot is created only after Verification succeeds.

```
Validation ✓ → Apply ✓ → Verification ✓ → Snapshot 13
```

**Invariant: a Snapshot represents a configuration that was validated, applied to the
actual Cluster, and verified as healthy.** This holds with no exceptions, including
Adoption (§15).

The main UI action is *Verify and Commit*, showing progress through the real stages:

```
Validating configuration        ✓ static checks, ✓ ephemeral pod
Applying configuration          ✓ catalogs via DDL, ✓ rollout
Verifying cluster               ✓ coordinator, ✓ workers, ✓ smoke query
Committing                      ✓ Snapshot 13 created
```

A separate Validate action lets Operators test a Candidate without applying it.

# 10. Failure handling

## Validation failure

Nothing was applied. The Candidate remains available to fix or Reset.

## Apply or Verification failure

**No Snapshot is created.** The latest Snapshot remains the known-good configuration, and
the latest-Snapshot pointer never moved.

**Auto Rollback** returns the Cluster to the latest Snapshot. Despite the name it is *not* a
Full Rollback — it is automatic, not an Operator action, and it produces no Snapshot:

- It re-renders the latest Snapshot's configuration, applies it, and attempts Verification
  **once**.
- For catalogs it issues compensating DDL — dropping what this Apply created, restoring
  what it dropped.
- It creates **no** Snapshot.
- It **never retries.**

If that single attempt fails, Apchi stops touching the Cluster:

- Prominent UI message: "The Trino cluster is currently unhealthy after applying
  configuration. Please contact our team."
- An internal alert (e.g. to a Mattermost channel).
- **Maintenance Mode engages automatically**, so nobody edits a Cluster in an unknown state.

Two consecutive verification failures mean the problem is not the configuration. That is an
operational incident, not a validation error.

## Apply is not atomic

The two file engines are recoverable: a write either lands or it does not, and an Apply
touching several files is not atomic across them but is always repairable by writing them
again — the desired state is declarative.

**DDL apply is not.** Four catalog changes, third one fails, and the Cluster matches neither
the previous Snapshot nor the Candidate — and no rewrite repairs it, because the change was
imperative.

Apply therefore orders **creates and alters before drops**, so a partial failure leaves a
superset of both states — an unused catalog breaks nobody, a missing one breaks every query
against it. See [ADR-0004](./docs/adr/0004-non-atomic-apply.md).

Discarding before Apply still leaves production unchanged. Apply itself is not atomic, and
this document does not imply otherwise.

## Divergence between the catalog Secret and Trino's store

Catalogs exist in two places: the Secret, which is durable, and Trino's store directory, which
is live (§7.1). Three ways they diverge, and what each looks like:

| Cause | State | Surfaces |
|---|---|---|
| Secret patched, DDL failed | recorded, not live | immediately — the DDL returned an error |
| DDL succeeded, Secret patch failed | live, not recorded | at the next pod restart, as a vanished catalog |
| `DROP CATALOG` issued outside Apchi | recorded, not live | at the next pod restart, as a returning catalog |

The first is why Apply patches the Secret **before** issuing the DDL: the failure is visible at
once and retryable. The second is the dangerous ordering — a catalog that works today and
disappears weeks later, with nothing linking the two events. The third is why catalog DDL is
restricted to Apchi's identity (§17).

Verification catches all three while the Apply is still in flight, because step 4 compares
`SHOW CATALOGS` against the Candidate (§8). Mongo remains the single source of truth; the
Secret and the store are both projections of it, and Auto Rollback restores both from the
latest Snapshot.

# 11. Recovery: Section Revert and Full Rollback

## Section Revert — the routine action

An Operator replaces one Section of the Candidate with its content from an earlier Snapshot,
leaving every other Section untouched.

```
POST /api/v1/resource-groups/revert  {"snapshot": 10}
```

This **stages into the Candidate**. It does not apply directly — it then needs an ordinary
`POST /applies`, like any other edit. That keeps the rule that nothing bypasses the pipeline,
and lets an Operator revert one Section and adjust another before applying once.

Granularity is the Section. Per-resource revert is a natural follow-on but multiplies the
diff surface; it is not in the first version.

**A Section Revert produces a configuration that has never existed.** Reverting resource
groups to Snapshot 10 while everything else stays at Snapshot 13 yields a novel combination
that was never validated or run anywhere. It becomes Snapshot 14 after the full pipeline.
The UI must say what it actually does — "resource groups will be restored to Snapshot 10;
all other sections stay at Snapshot 13" — and never imply a return to a known-good state.

For catalogs, a revert issues real `CREATE` and `DROP` statements against a running Cluster.
The warning must be specific: "3 catalogs will be dropped" is materially different from
"resource groups will be rewritten".

## Full Rollback — disaster only

Replaces the entire Candidate with an earlier Snapshot. Never implicit; always an explicit
Operator choice.

Rollback does not rewrite history. The selected Snapshot is loaded as the desired target,
validated, applied, verified, and committed as a **new** Snapshot:

```
Snapshot 13 (current) → restore Snapshot 10 → Snapshot 14
```

Snapshot 10 itself is never modified.

Restoration is declarative within the scope of the Snapshot: the question is "how do I make
the managed system match this?", not "how do I reverse each historical operation?"

## External systems

Snapshots represent Apchi-managed configuration only. If an old Snapshot references
something that no longer works because the external world changed — a database moved,
credentials rotated at the source, a remote service disappeared, a schema changed — that is
outside Apchi's responsibility. The Snapshot guarantees restoration of Apchi-managed
configuration, not restoration of the external world.

# 12. Secrets

Snapshots store **actual secret values**, not references. Section Revert is what makes this
workable: an Operator recovering a broken Section is never forced to also restore an old
certificate or credential, because they revert only the Section they need.

All Apchi users — Operators and Admins — are trusted, so there is no requirement to hide
secret values from them.

Two consequences, recorded rather than solved:

- **Snapshots are an append-only store of every credential the Cluster has ever used.** A
  request to purge a compromised key cannot be honoured without breaking Snapshot
  immutability.
- Because Mongo holds the only copy of every Snapshot (§18), **Mongo backups are also a
  historical credential store.** Encryption at rest and backup access control are doing real
  security work here, not just hygiene.

# 13. Managed areas

## 13.1 Catalogs

Apchi exposes Trino catalogs as the Operator-facing resource. A Catalog is the
Trino-visible object; a Connector is the plugin it uses to reach an external system.

```
GET    /api/v1/catalogs
POST   /api/v1/catalogs
GET    /api/v1/catalogs/{id}
PATCH  /api/v1/catalogs/{id}
DELETE /api/v1/catalogs/{id}
```

Applied via DDL — see §7.1.

### Connector schemas

Apchi ships a **typed model per supported connector** — required fields enforced, unknown
properties rejected, so a typo like `connection-uri` for `connection-url` fails at request
time rather than at Apply. This is what lets the UI render a real form instead of a key-value
grid, and it is most of what §1 means by working with high-level concepts.

Connectors outside that set fall back to **pass-through key-value properties**, validated only
by the ephemeral pod at Apply. The API marks these as unsupported so nobody mistakes the
escape hatch for a curated path.

**The curated set:**

| Connector | `connector.name` |
|---|---|
| PostgreSQL | `postgresql` |
| MongoDB | `mongodb` |
| Hive | `hive` |
| Iceberg | `iceberg` |
| Redis | `redis` |
| Elasticsearch | `elasticsearch` |
| Kafka | `kafka` |

Seven schemas is a real but bounded commitment, and each drifts with Trino versions — so the
supported Trino range §8 pins is what bounds the maintenance. Everything outside this set
passes through.

**Credential exposure, accepted:** credentials inlined in `CREATE CATALOG` are delivered
verbatim to any configured event listener endpoint. `QueryMetadata` carries the full SQL
text, `QueryMonitor` passes it through untouched, and the HTTP event listener serialises the
whole event and POSTs it. Trino provides no redaction mechanism. The query-visibility rules
in §13.3 close the Web UI and `/v1/query` paths; they do not close this one. Accepted, not
solved.

## 13.2 Client Certificates

The certificate and private key Trino presents when it connects **outward** to an external
system — a PostgreSQL-backed catalog, an event-listener endpoint requiring mTLS. Trino is the
client here. This is a different thing from §13.3, which is about callers authenticating
inward *to* Trino.

Operators never convert certificate formats themselves. They drag-and-drop a ZIP containing
the certificate and private key; Apchi extracts it, identifies the parts, validates that they
are a matching pair, performs any format conversion, and stores the result with its metadata.
Automatic renewal may follow later.

The UI exposes: name, CN, subject, issuer, expiration, status. Expiry is first-class —
`GET /api/v1/certificates?status=expiring`.

### Delivery

Trino consumes these as **files on disk**, because connector properties reference paths:

```
connection-url = jdbc:postgresql://host:5432/db
  ?sslcert=/etc/trino/certs/finance.crt&sslkey=/etc/trino/certs/finance.pk8&sslmode=require
```

**One Kubernetes Secret holds every client certificate**, mounted once at a fixed directory on
the coordinator and on workers. See
[ADR-0005](./docs/adr/0005-single-certificate-secret.md). Adding a certificate adds a key to
that Secret, so the file
appears in an already-mounted directory — the pod spec does not change, and **no restart is
required**.

The alternative — a Secret and a volume per certificate — would change the pod spec on every
upload, restarting the Cluster and destroying every running query because somebody uploaded a
certificate.

A catalog references a certificate by name; Apchi renders the path into the generated
connector properties.

### Ordering

The certificate must exist on disk before any catalog that references it connects. Apply
therefore writes the Secret, waits out the kubelet sync (§7.5), and only then issues the
catalog DDL. Reversing the order points a catalog at a file that is not there yet.

This is the second reason §16's `subPath` precondition is load-bearing: a `subPath`-mounted
certificate directory never receives new files, so every certificate added after pod creation
would silently fail to appear.

## 13.3 Certificate Mapping

How a caller authenticating **inward** to Trino becomes a Trino Identity. No private key is
involved — the caller holds that; Apchi configures the derivation.

**One Certificate Mapping Pattern, Operator-configurable.** Operators do not maintain
arbitrary independent mappings; Apchi exposes a single controlled pattern, which removes the
need for a mapping entry per identity.

```
<identity>.clients.example.com     → <identity>
trino-<identity>.customer.internal → <identity>
```

When the pattern changes, the previous one remains valid for a defined grace period. Trino's
user-mapping **file** format supports multiple rules evaluated top-to-bottom, first match
wins, so a grace period is expressible directly as two rules — one per pattern.

Changing the pattern is a breaking operational change regardless: clients must obtain new
certificates, deploy them, and migrate before the grace period ends. Allowing Operators to
choose the destination pattern does not remove that migration; it gives freedom over the
convention at the cost of more operational responsibility. This trade was made deliberately,
and the migration tooling must account for it.

**Rollout-required.** `UserMapping` parses an immutable rule list at authenticator
construction, so entering and leaving a grace period each cost a coordinator restart.

**Identity flow:**

```
Caller certificate → Certificate Mapping Pattern → Trino Identity → Permissions → Resource Group
```

Authentication and authorization stay separate. **A mapped Trino Identity does not receive
access.** Generated authorization configuration is fail-closed.

## 13.4 Permissions

Apchi exposes a high-level Permissions interface; Operators never edit Trino authorization
JSON.

```
Identity: acme_finance
Catalog:  iceberg
Schema:   finance
Table:    transactions
SELECT ✓   INSERT ✗   UPDATE ✗   DELETE ✗
```

**Query visibility.** By default any authenticated End User can read every other End User's
SQL — the Web UI docs state "If no system access control is installed, then all users are
able to view and kill any query", and `/v1/query` is annotated `@ResourceSecurity(AUTHENTICATED_USER)`
and returns unredacted query text. Query text routinely contains data.

Apchi therefore injects a `queries` rules block: `execute` for everyone, `view` and `kill`
restricted to the query owner, plus the reserved verification identity.

This must be **visible** in the Permissions UI as a system-owned rule, not a silent default,
for two reasons: Operators need to understand why they cannot see each other's queries and
be able to widen it deliberately; and the block is all-or-nothing — the moment a `queries`
section exists, anything unmatched is denied, *including `execute`*. A mistake here does not
leak data, it stops the Cluster serving queries.

**No restart required** — Trino re-reads the rules file on its own timer, so a permission
change reaches the Cluster without touching the pod (§7.2).

`security.refresh-period` must be set on the Cluster, or the rules file is read once at
startup and never again. Apchi asserts this at Adoption and fails loudly if absent —
otherwise Apchi will believe it applied permissions Trino never read.

**Permission observability** is a goal: explain what an identity can access, and which
identities can access a given resource, without anyone reading access-control files.

## 13.5 Resource Groups

Operators configure resource group hierarchies, selectors, limits and their interaction with
Trino Identities.

**Rollout-required.** `FileResourceGroupConfigurationManager` parses its file once, in its
constructor: no timer, no watcher. A change needs a coordinator restart. (The DB-backed
manager polls every second and would make this Section dynamic; that is a future migration,
not the current design.)

## 13.6 Event Listeners

Operators configure event listeners by type. Like catalogs, the properties are defined by
whichever plugin implements the listener, so the same curated-plus-pass-through rule applies
(§13.1).

**The curated set — Trino's built-in listeners:**

| Listener | `event-listener.name` |
|---|---|
| HTTP | `http` |
| Kafka | `kafka` |
| MySQL | `mysql` |
| OpenLineage | `openlineage` |

Custom listener plugins pass through as key-value properties, validated only by the ephemeral
pod at Apply. As with connectors, the supported Trino range (§8) is what bounds the
maintenance on these schemas.

**Rollout-required.** `EventListenerManager.loadEventListeners()` is guarded by a
`compareAndSet` that permits exactly one call per process lifetime. There is no reload path.

During editing the UI should say: *Event Listener changed — does not apply immediately, will
apply during Apply, and will restart the cluster.*

# 14. Admin capabilities

## Arbitrary Trino configuration

Admins can add arbitrary low-level configuration values written directly into generated Trino
configuration files. An Admin-only escape hatch, not exposed to Operators.

**Stored in MongoDB as configuration, and rendered into the same ConfigMaps and Secrets as
Operator configuration — but never part of a Snapshot.** Snapshots are the history of
Operator-managed configuration; Admin values are platform state with a different lifecycle.

**Admin wins on conflict.** Where an Admin value and generated Operator configuration set the
same property, the Admin value takes effect. It is an escape hatch used during upgrades and
incidents; one an Operator could override would not be an escape hatch.

### Admin Apply

An Admin change gets its own Apply, running the same pipeline — Validate on the ephemeral pod,
render, roll out, Verify — but **creating no Snapshot**.

Arbitrary low-level values are precisely the class of configuration most able to stop Trino
booting, so they go through the same validation as everything else rather than straight to the
Cluster. And an Admin fixing something during an incident cannot be made to wait for an
Operator to apply something, so the change cannot simply sit in MongoDB until the next
Operator Apply.

An Admin Apply **freezes the Candidate** exactly as an Operator Apply does: both mutate one
Cluster, and the pipeline's guarantees depend on nothing else changing underneath.

Still to decide: which files and properties may be targeted, and how secrets among these
values are handled.

## Maintenance Mode

Admins can disable and re-enable Operator mutation. The primary use is platform upgrades and
maintenance, where Operator changes must be frozen while the environment is modified. It
also engages automatically after a failed Auto Rollback (§10).

While frozen:

- Operator read access continues
- All Operator mutations are rejected with a clear structured error stating that changes are
  temporarily disabled by an Admin
- Admin operations are unaffected

Not yet decided: the endpoint path, persistence model, and behaviour for an Apply already in
flight when an Admin engages the freeze.

## Ownership boundary

The boundary is role-based. Operators manage the six Sections. Admins own infrastructure
and the low-level escape hatches needed to operate the platform. Most infrastructure
configuration stays outside the Operator-facing model entirely.

# 15. Adoption

Onboarding a Cluster whose configuration predates Apchi is an explicit lifecycle stage, not
a special case of Commit.

Existing configuration is imported into the Candidate, validated, and taken through a real
Apply and Verification to produce **Snapshot 1**.

This keeps §9's invariant literally true with no exceptions. The alternative — an "adopted"
Snapshot flagged as never-verified — would put a permanent asterisk on the one property that
makes Rollback trustworthy. An adopted Snapshot 1 is not known-good until it has been
applied once.

Adoption also asserts the Cluster's preconditions (§16) and fails loudly if any are unmet.

# 16. Kubernetes integration

Apchi asserts preconditions on the Trino deployment rather than owning its manifests.
Owning them would put Apchi in the business of heap sizes, node selectors and image tags,
which belongs to Admins.

**The precondition that must be checked: no Apchi-managed configuration file is mounted with
`subPath`.** Kubernetes documents that "a container using a ConfigMap as a `subPath` volume
mount will not receive ConfigMap updates" — the file is frozen at pod creation, permanently.
Apchi would write the ConfigMap, see the write succeed, report success, and Trino would never
see the change.

Checked at Adoption **and before every Apply**, because a later chart change can reintroduce
it silently.

**The second precondition: the catalog seed initContainer is present**, and
`catalog.config-dir` is a plain container path rather than a mounted volume (§7.1). Without
the initContainer the Cluster comes up with no catalogs; with a volume mounted there, Trino
cannot write and every `CREATE CATALOG` fails.

## Rollout mechanics

The coordinator Deployment uses `RollingUpdate` behind a single Service. The consequences
are understood and accepted — see
[ADR-0003](./docs/adr/0003-coordinator-rollout.md). Draining will be solved later by putting
Trino Gateway in front of two full clusters and cutting over blue/green.

Note that without a PodDisruptionBudget on the coordinator, an ordinary Kubernetes node drain
can take it down mid-query at any time, entirely outside Apchi.

# 17. Configuration drift

Apchi does not detect configuration drift caused by changes made outside it. Detecting,
alerting on, reconciling or adopting externally modified state is out of scope. External
modifications are the responsibility of the surrounding platform and its operational
processes.

Admin-provided configuration through the supported Admin mechanism is not drift.

Note the one place this is load-bearing: catalog DDL is restricted to Apchi's identity
precisely because a `DROP CATALOG` issued outside Apchi permanently deletes a file Apchi
believes it owns, and nothing would detect it.

# 18. Storage

MongoDB stores all Apchi-managed configuration: complete immutable Snapshots plus the
current Candidate.

This keeps persistence separate from Trino implementation details. If a mechanism changes
later — resource groups moving from a file to an external database, permissions moving to
OPA — the Operator-facing model need not change.

**Mongo holds the only copy of every Snapshot and the entire audit trail.** If it is lost,
the Cluster keeps running configuration nobody can reproduce. This is the real disaster
scenario the design creates, and it is not currently addressed.

# 19. API design

## Versioning

The contract must be stable, because Operators automate against it.

```
/api/v1/catalogs
/api/v1/certificates
/api/v1/certificate-mapping
/api/v1/permissions
/api/v1/resource-groups
/api/v1/event-listeners
/api/v1/review
/api/v1/applies
/api/v1/validations
/api/v1/snapshots
```

Admin capabilities (§14) sit on a separate Admin-only surface — arbitrary Trino
configuration, its own Apply, and Maintenance Mode. Operators never see these paths, and the
stability promise above is for the Operator API; the Admin surface may move faster. The
paths themselves are still to be defined.

A policy is still needed for breaking changes, deprecated endpoints, deprecated fields and
new API versions.

## Applies

An Apply runs Validation, Apply, Verification and Commit — minutes, not a request. It is
therefore a resource, not a synchronous call:

```
POST /api/v1/applies              → 202 { "id": "apl_...", "stage": "validating" }
GET  /api/v1/applies/{id}         → current stage, full stage history, failure reason
GET  /api/v1/applies/{id}/events  → SSE stream of stage transitions
GET  /api/v1/applies              → history
```

## Validations

The Validate action of §9 is a resource for the same reason: bringing up the ephemeral
coordinator takes as long as it takes.

```
POST /api/v1/validations          → 202 { "id": "val_...", "outcome": "running" }
GET  /api/v1/validations/{id}     → outcome, and every failure with its resource and reason
GET  /api/v1/validations          → history
```

It runs the Apply pipeline's Validation stage and stops there, so the Validate action cannot
drift from the Validation an Apply performs — it is the same code path. A Validation does
**not** freeze the Candidate: it changes nothing, so there is nothing for a concurrent edit to
corrupt.

Two properties matter more than the transport. The Apply record lives in **MongoDB**, so the
stream is a view over durable state rather than in-memory progress. And the record holds the
**full stage history**, not just the current stage, so a client reconnecting mid-rollout
replays what it missed instead of showing a blank timeline.

**Startup recovery.** The Candidate is frozen during Apply (§4), so an Apchi crash mid-rollout
would freeze it permanently. On startup Apchi scans for Applies left in flight and resolves
them: a rollout re-enters Verification and the normal failure path handles it; partially
applied catalog DDL goes to the incident state of §10 rather than having a machine improvise
compensating statements against an unknown partial state. Either way the Candidate is
unfrozen, which is the part that must never be left to chance.

## Authentication

The UI uses the normal SSO model. Machine-to-machine access uses tokens issued through the
UI.

## PATCH semantics

Must be explicitly defined and consistent across every endpoint — whether a partial body
updates only the named fields, uses JSON Merge Patch, uses JSON Patch, or uses custom
semantics.

## Idempotency

Clients retry after network failures. `POST /applies`, catalog creation, certificate upload
and rollback all need an idempotency mechanism so a retry does not duplicate work.

## Errors

Structured errors with a machine-readable code, human-readable message, details, and a
request ID. **The code is what clients branch on**; the status is a coarse signal and cannot
carry the distinction between, say, an unknown connector and a missing certificate reference.

| Case | Status |
|---|---|
| Malformed JSON | `422` |
| Missing, wrong-typed or unknown field | `422` |
| Semantic failure — unknown connector, missing reference | `422` |
| Name already taken | `409` |
| Mutation while the Candidate is frozen (§4) | `409` |
| Mutation under Maintenance Mode (§14) | `409` |

Strictly, RFC 9110 puts malformed content at `400` and well-formed-but-unprocessable at
`422`. FastAPI returns `422` for both, because a JSON decode failure surfaces as a Pydantic
validation error. Reclaiming that distinction costs an exception handler for something almost
no client branches on, so the default stands.

## Collections

One consistent pagination, filtering and sorting model.

```
GET /api/v1/certificates?status=expiring
GET /api/v1/snapshots?limit=20
GET /api/v1/catalogs?connector=iceberg
```

Likely large collections: snapshots, audit events, permissions, certificates, catalogs.

# 20. Audit history

Snapshots and audit history answer different questions. Snapshots: *what complete
configurations were committed?* Audit: *who changed what, and when?*

Audit events record actor, time, Section, resource, change, resulting Snapshot, and outcome.
Because the Candidate is shared, the audit trail is the only record of which Operator staged
which change — it is what makes the concurrency trade in §4 acceptable.

**Admin changes are audited too**, and they are not a Section — so the Section field needs a
category for them, and they carry no resulting Snapshot because an Admin Apply creates none
(§14). Without this the audit trail silently omits the one class of change that can alter the
Cluster without appearing in any Snapshot.

# 21. UI

The UI always shows:

- Cluster health and current Snapshot
- Whether the Candidate has uncommitted changes, and by whom
- The full Candidate diff before Apply, every Section, not only the current Operator's
- Which changes apply without a restart and which force one
- Available actions: Reset, Validate, Verify and Commit

When a Candidate requires a rollout, the Apply confirmation must state that running queries
will be terminated, in those words, with a live running-query count from the coordinator.

Staged changes must be clearly marked as not yet affecting production.

## Failure messages

**Validation failed** — "Resource Groups configuration could not be loaded by Trino. No
changes were applied."

**Apply failed** — "Configuration application failed. The previous configuration is being
restored."

**Verification failed** — "Configuration was applied, but the Trino cluster did not pass
health verification. The previous Snapshot remains the known-good configuration. Please
contact our team if the cluster does not recover." Plus an internal alert and automatic
Maintenance Mode.

# 22. Technology

Chosen for what the team already runs, not for what scores best in isolation. Where those
differ, familiarity wins — noted below where it does.

**Backend: Python + FastAPI.** The team knows it. Conventional Python throughout.

**Kubernetes: the official `kubernetes` client, called through a threadpool.** The official
client is synchronous only — its in-tree async work is alpha and does not support watch or
streaming — so calling it directly from an async handler blocks the event loop. Wrap every
call in `run_in_threadpool` (AnyIO's 40-thread default is not a constraint for a control
plane doing a handful of API calls per Apply). `kubernetes_asyncio` is technically the better
shape but far less recognised; the official client is what every example and answer assumes.
Keep all Kubernetes access behind one module so swapping later is a change in one place.

**Trino: the official `trino` package**, also synchronous, also via a threadpool. Only
`/v1/statement` is documented public API, so `/v1/info`, `/v1/status` and `/v1/query` go
through plain `httpx` — behind the single adapter §8 already requires. Cluster membership comes
from the `system.runtime.nodes` system table over the SQL connection, not a REST endpoint.

**MongoDB: PyMongo's Async API**, optionally with Beanie for Pydantic-native documents.
**Not Motor** — MongoDB deprecated it in May 2025 with end of life in May 2026, and it is now
in critical-fixes-only support until May 2027. Beanie itself migrated off Motor in 2.0.0.
Most FastAPI+Mongo tutorials still say Motor; they are out of date.

**Progress streaming: FastAPI's native Server-Sent Events** —
`from fastapi.sse import EventSourceResponse, ServerSentEvent`. No third-party dependency;
`sse-starlette` is redundant on current FastAPI, though engineers will reach for it out of
habit. SSE rather than WebSockets because progress is one-way.

**OpenAPI: code-first, with a committed spec and a CI gate.** FastAPI generates the spec from
code and has no supported spec-first workflow. Commit the generated `openapi.json`; CI
regenerates and diffs it, failing when they differ. That forces every contract change into the
pull request diff where a reviewer sees it — which is the property §19's stability requirement
actually needs. Add `openapi-spec-validator` for well-formedness.

**Testing: two tiers.** Testcontainers (`testcontainers.community.trino` — the top-level
`testcontainers.trino` path is deprecated) for everything single-node: catalog DDL, permission
reload timing, validation-pod behaviour, smoke queries, the `queries` rules block. A **kind**
cluster in CI for the rollout path — ConfigMap propagation, restart, worker rejoin,
verification, Auto Rollback. The second tier is slow and skippable, and it is the only place the
riskiest engine is exercised. Pin the Trino version to the range §8 declares supported so an
upgrade breaks CI rather than production.

**Local development: kind**, same as CI, so there is one thing to learn.

**Frontend: a TypeScript SPA consuming the public REST API**, with no privileged backdoor —
which makes §1's "everything in the UI is in the API" structural rather than a rule someone has
to remember. Generate its client from the committed spec. Built only after the backend works
end to end.

# 23. Code conventions

Optimised for comprehension by the people maintaining this, not for concision. Where a
conventional choice and a clever one differ, the conventional one wins.

## Layout

The directory tree states the architecture:

```
app/
  pipeline/        candidate, review, validate, apply, verify, commit, auto_rollback
  sections/
    catalogs/              model, generator, apply strategy, restart requirement
    client_certificates/
    certificate_mapping/
    permissions/
    resource_groups/
    event_listeners/
  adapters/        kubernetes, trino, mongo
  api/             routers
```

Layering by kind — `models/`, `services/`, `repositories/` — would scatter one Section across
four directories, so understanding catalogs would mean opening all of them. Organising purely
by feature would hide the pipeline, which is where the risk lives. This does neither: the
pipeline is one place, and each Section is a uniform plug-in providing exactly four things —
a model, a configuration generator, an apply strategy, and whether it requires a restart.
"What must a Section provide?" is answerable by listing one directory.

## Vocabulary

The terms in [CONTEXT.md](./CONTEXT.md) are the literal names in code: `Snapshot`,
`ConfigurationCandidate`, `Section`, `apply`, `rollout`, `validate`, `verify`, `commit`,
`reset`, `section_revert`, `full_rollback`, `auto_rollback`, `maintenance_mode`,
`TrinoIdentity`, `CertificateMappingPattern`.

Where the glossary defines a term, no synonym for it appears in the code. A term that means
one thing in the glossary and something else in the code is worse than having no glossary.

## Types

Strict type checking in `pipeline/` and `sections/`, gated in CI. Relaxed in `adapters/`,
where the Kubernetes client's generated types are poor enough that a strict gate buys
arguments with the type checker rather than safety — the same boundary that contains the
threadpool.

Pydantic models, never dictionaries, for anything crossing a module boundary. Unknown fields
are rejected rather than persisted. Where two domain strings could plausibly be confused — a
catalog name, a certificate CN, a Section name — they get distinct types.

## Comments

**Comment the constraint, not the mechanism.**

```python
# kubelet takes up to syncFrequency (60s, default) to project a ConfigMap change
# into the pod, and Trino then re-reads the rules file on its own
# security.refresh-period timer. The two delays add. See §7.5.
await self._wait_for_rules_reload()
```

This design rests on Trino and Kubernetes behaviour no reader could infer from the code: that
the Apply-to-Verify wait is kubelet sync *plus* refresh period; that creates must precede
drops; that a readiness probe passing does not mean workers rejoined. Every such dependency
gets a comment naming the behaviour and the section that argues it. A bare `sleep(65)` reads
as superstition and someone will remove it.

A comment restating what the line does is noise. The "why" is the only part that cannot be
recovered by reading the code.

## Errors

No bare `except`. No swallowing. A failure inside a loop fails the operation — catching per
item and logging produces an operation that reports success having done nothing, which is
indistinguishable from working until someone checks the cluster.

## Async boundary

The Kubernetes and Trino clients are synchronous, so `run_in_threadpool` lives in
`adapters/` only. Pipeline code and route handlers are plain `async` and never mention
threads.

## Observability

The goal is that a runtime failure is diagnosable from the logs without reproducing it.

**Level.** A `LOG_LEVEL` setting, defaulting to `DEBUG` in np and test and `INFO` in prep and
prod, overridable at runtime without a rebuild. The level is wanted most during a production
incident, so it must not be a pure function of environment type.

**Correlation.** Stdlib `logging`. A `contextvar` holds the id of the Apply in progress and a
filter injects it into every record, so filtering on one id yields the complete story of a
failure. During an Apply several things run concurrently — polling `/v1/query`, watching a
rollout, polling node state — and without correlation the lines interleave and no log level
makes them readable. JSON formatter in-cluster, plain console formatter locally, selected by
the same setting.

**Secrets.** Snapshots hold actual secret values (§12), so:

- A redaction filter keyed on field names known to carry secrets runs at **every level in
  every environment**. Never conditional — a conditional redactor is one configuration
  mistake away from not redacting.
- Rendered configuration is **never logged wholesale**, at any level. Log resource names and a
  diff summary instead: `permissions: 3 rules changed`, `catalogs: +finance_pg`.
- There is no environment where dumping credentials is acceptable. Non-production
  certificates and database passwords are real credentials to real systems.

**What goes where.** INFO carries stage transitions, the computed restart set, what changed,
and the decision taken. DEBUG carries the DDL issued with values redacted, poll results,
Kubernetes API calls, and timings.

**Failures reach the Operator, not only the logs.** The Apply record carries the failure
reason, not just the stage it died at — otherwise diagnosing any failed Apply requires an
engineer with log access, when the Operator who triggered it could often read the answer
themselves.

## Tooling

`ruff` for both formatting and linting. Line length 100, matching this document.

# 24. Build order

The first slice is one Section taken end-to-end against a real Trino, with nothing else
built: the Candidate and its persistence, `review`, `applies` driving Validate → Apply →
Verify → Commit, Snapshot creation, Section Revert, Reset, and Auto Rollback on failure.

**Slice 1 — Catalogs.** They carry the sharpest model questions (non-atomic Apply,
compensating Auto Rollback), they are the highest-value Section, and they can be iterated on
without restarting the coordinator or destroying anyone's queries during development.

**Slice 2 — Event Listeners, before any breadth.** Catalogs prove the Candidate machinery
and the DDL Apply engine, but leave the rollout Apply engine — restart, worker rejoin,
verification after restart, incident state, Maintenance Mode — completely unproven, and
three of six Sections depend on it. Going catalogs → permissions → resource groups would build
most of the product before discovering whether the rollout path works.

Not in the first slices: the UI, Adoption, Maintenance Mode, audit history, pagination.

# 25. Core invariants

1. Snapshots are immutable.
2. A Snapshot is created only after its configuration passed validation, was applied to the
   actual Cluster, and passed verification. No exceptions, including Adoption.
   **A Snapshot records Operator-managed configuration, not the whole Cluster**: what ran was
   the Snapshot merged with the Admin values current at the time (§14). Restoring it later
   merges it with today's Admin values — a combination that was never verified together.
   Admin values deliberately survive a rollback; they are platform state, and an Operator's
   rollback should not revert them.
3. Nothing reaches the Cluster before Apply. Reset before Apply therefore guarantees
   production was never touched.
4. There is exactly one Configuration Candidate per Cluster, and it is frozen for the whole
   of any Apply — Operator or Admin.
5. The file engines are declarative and repairable by rewriting. DDL apply can partially
   succeed, so Apply orders creates before drops.
6. Auto Rollback is bounded to one attempt, creates no Snapshot, and escalates to an incident
   with Maintenance Mode on failure.
7. Section Revert and Full Rollback both go through the full pipeline and produce new
   Snapshots. History is never rewritten.
8. Authentication and authorization are separate. A mapped Trino Identity does not receive
   access. Generated authorization is fail-closed.
9. Every Section participates in the Candidate and Snapshot model. Admin arbitrary
   configuration does not: it is applied through the pipeline but never recorded in a
   Snapshot (§14).
10. Validation decides whether configuration should be applied; Verification decides whether
    the Cluster adopted it. They are not interchangeable.
11. Apchi manages exactly one Cluster.

# 26. Trino constraints that shape this design

Verified against Trino documentation and source. These are the facts the design is built
around; changing any of them would reopen a decision.

| Subsystem | Reloads at runtime? | Evidence |
|---|---|---|
| Catalogs (`catalog.management=dynamic`) | Yes, via SQL DDL | `CREATE`/`DROP CATALOG`; docs mark it experimental |
| File-based access control | Only if `security.refresh-period` is set — **no default** | Guava `memoizeWithExpiration`; lazy on next check |
| Resource groups (file) | **No** | `FileResourceGroupConfigurationManager` parses once in its constructor |
| Event listeners | **No** | `EventListenerManager.loadEventListeners()` has a `compareAndSet` guard |
| Certificate mapping | **No** | `UserMapping` builds an immutable rule list at construction |
| Coordinator | No drain, no HA | `NodeStateManager` throws "Cannot drain coordinator"; trinodb/trino#391 open since 2019 |

Additional constraints:

- **`nextUri` has no pod affinity.** It is rebuilt from the incoming request's base URI, and
  query state is a per-process map, so a poll reaching the wrong coordinator returns 404
  "Query not found".
- **Workers announce every 5s with a 30s in-process TTL**, so two coordinators behind one
  Service each see a partial, shifting subset.
- **No endpoint reports which configuration is loaded.** Verification must be functional.
- **`/v1/node` does not exist on Trino 483** (404). Cluster membership comes from the
  `system.runtime.nodes` system table.
- **ConfigMap volume updates are watch-driven** — normally a few seconds, bounded by the
  kubelet's `syncFrequency` (default 1 minute); `subPath` mounts never update at all.
- **Fault-tolerant execution does not survive a coordinator restart** — it recovers from
  worker failure only.
- **Dropping a catalog leaks resources** in the Hive, Iceberg, Delta Lake and Hudi
  connectors.
- **`FileCatalogStore` enumerates its directory only in its constructor.** Catalog files
  written while the coordinator is running are never picked up until it restarts — which is
  why catalogs are applied by DDL rather than by writing files.

# 27. Open items

- **Admin arbitrary configuration** — which files and properties may be targeted, and how
  secrets among those values are handled (§14). Storage, precedence, validation, audit and
  Snapshot scope are settled.
- **Maintenance Mode mechanics** — endpoint, persistence, and behaviour when an Apply is
  already in flight.
- **Certificate mapping migration** — the procedure for moving an existing Cluster onto a
  single Certificate Mapping Pattern is undefined: sequencing, authorization edge cases,
  naming and domain constraints, grace-period duration, rollback, and whether every existing
  setup can migrate cleanly. Note each pattern change costs a coordinator restart (§13.3).
- **Field-level models for Resource Groups and Event Listeners** — hierarchy, selectors and
  guardrails for the former; supported types, schemas and plugin requirements for the latter.
- **Event listener node scope** — whether workers genuinely need event listener configuration
  is unresolved in Trino's documentation.
- **Apchi's own disaster recovery** — Mongo is the single copy of all Snapshots and audit
  history (§18).
- **API policy** — breaking changes, deprecation, new versions.
- **Draining** — deferred to Trino Gateway blue/green.

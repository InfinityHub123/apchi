# Apchi

Apchi is a control plane for configuring one Trino cluster. The people running Trino
configure catalogs, permissions, resource groups, event listeners and certificates through a
REST API, and never hand-edit a Trino configuration file or restart a coordinator by hand.

Every change goes through the same loop:

```
stage → review → validate → apply → verify → commit
```

Nothing reaches the cluster until you apply. Validation runs the configuration past a real
throwaway Trino first, so a coordinator that would refuse to start fails the validation
rather than your cluster. An apply that fails verification rolls the cluster back by itself.
What survives is a numbered, immutable **Snapshot** you can roll back to.

Documentation: **<https://infinityhub123.github.io/apchi/>**, built from `main`.

## What Apchi configures

| Section | What it covers | What an apply costs |
| --- | --- | --- |
| Catalogs | Data sources and their connector properties | Nothing — issued as `CREATE CATALOG` DDL |
| Permissions | Which identity may read which catalog, schema and table | Nothing — a file Trino re-reads on a timer |
| Client certificates | Certificates Trino presents to external systems | Nothing — a new file in a mounted directory |
| Certificate mapping | How a caller's certificate becomes a Trino identity | A coordinator restart |
| Resource groups | Concurrency, memory and CPU limits, and what lands where | A coordinator restart |
| Event listeners | Where Trino sends query events | A coordinator restart |

Review tells you which of those a pending change is before you run it, and — if it restarts
the coordinator — how many queries that will destroy.

Those six are the Operator's. An **Admin** can do everything an Operator can, and also write
raw Trino configuration files that Apchi delivers without forming an opinion about, and engage
Maintenance Mode, which keeps read access and refuses every configuration change (§14).

## How it fits together

Apchi is one container with no state of its own. It sits between you and three things:
MongoDB, where it keeps its Snapshots; the Kubernetes API, where Trino's configuration
lives as Secrets; and Trino itself, which it talks to as an ordinary SQL client.

```
   you ──REST──▶ ┌───────┐ ──Snapshots, Candidate, Applies──▶ MongoDB
                 │ Apchi │
                 └───────┘ ──patches Secrets, restarts the ──▶ Kubernetes API
                     │       coordinator, runs a probe pod         │
                     │                                            │ the kubelet
                     │ CREATE CATALOG, smoke query,               │ projects them
                     │ running-query count                        ▼
                     └──────────────────────────────▶ Trino coordinator
                                                        reads its configuration
                                                        from the mounted files
```

Each of those connections can be TLS, and a coordinator that authenticates its callers
requires it. Apchi takes a **URL** for the coordinator rather than a host and a port, so
`https://trino:8443` means what it says; it presents a client certificate to a Trino that
authenticates by one, and to a MongoDB that does; and a private certificate authority is one
setting covering every destination — the coordinator, MongoDB, your identity provider and the
alert webhook — **added to** the public trust store rather than replacing it, so trusting your
own CA cannot break a public one. [`docs/getting-started.md`](docs/getting-started.md) has the
values.

Nothing about Trino's deployment belongs to Apchi. The Admin owns the Deployment, the heap
sizes and the image; Apchi owns the configuration files inside it, delivered through Secrets
the coordinator mounts. That is the whole coupling, and it is why installing Apchi is just
installing Apchi — there is no sidecar, no operator and no webhook.

So two things have to agree. **Names**: the Secrets Apchi writes must be the Secrets the
coordinator mounts — the chart's `secrets.*` values are those names, and `deploy/trino-dev/`
is a Trino that mounts exactly them. And **mount shape**: most of what Apchi writes goes into
a directory the Admin mounts, which works on any deployment, but Resource Groups and Event
Listeners are single-file mounts Apchi makes itself, because Trino pins their pointer files to
that directory and no property relocates them. So the coordinator has to mount `/etc/trino`
file by file, as the chart's Trino does; a deployment that mounts it whole, as the official
`trino/trino` chart does, is not supported (§16). Apchi checks all of this before an Apply and
names what is wrong rather than half-applying.

### What using it looks like

Apchi's API is the interface — there is no UI yet, so this is `curl`, your HTTP client, or
`localhost:8000/docs`. The shape is the same for every Section:

```sh
A=localhost:8000/api/v1
J='content-type: application/json'

# 1. Stage. Nothing reaches Trino.
curl -X POST $A/catalogs -H "$J" -d '{"name":"sales","connector":"tpch","properties":{}}'
curl -X POST $A/permissions -H "$J" \
  -d '{"identity":"analyst","catalog":"sales","privileges":["SELECT"]}'

# 2. See what an Apply would do, and what it would cost.
curl $A/review

# 3. Promote all of it, in one operation.
curl -X POST $A/applies
curl -N $A/applies/{id}/events     # follow the stages as they happen

# 4. It is now a Snapshot, and a Snapshot is something you can go back to.
curl $A/snapshots
curl -X POST $A/candidate/rollback -H "$J" -d '{"snapshot":1}'
```

Edits accumulate in one shared Configuration Candidate, so review shows you every Section
rather than only the one you touched — including anything a colleague staged. An Apply
promotes the lot.

## Install

The chart and the image publish to GHCR on every tag:

```sh
helm install apchi oci://ghcr.io/infinityhub123/charts/apchi --version 0.3.0 \
  --namespace trino \
  --set mongodb.uri=mongodb://your-mongo:27017
```

Or from a clone, which is what you want while changing Apchi itself — the chart then
installs whatever image you have built:

```sh
helm install apchi charts/apchi \
  --namespace trino \
  --set mongodb.uri=mongodb://your-mongo:27017
```

The release is Apchi and the Trino cluster it configures: a coordinator, its workers, and
the six Secrets Trino mounts its configuration from, created before Trino starts. Apchi is
told the same names the chart gives Trino, so there is nothing to line up. The chart defaults
to the image it was released with, for `linux/amd64` and `linux/arm64`.

`charts/apchi/README.md` documents every value and what the chart guarantees about its Trino;
the ones you are most likely to change are Trino's sizing and your MongoDB.

To try it on minikube or kind with nothing else set up, one command brings up the chart and a
MongoDB and tells you what to do next:

```sh
./scripts/quickstart.sh
```

## Requirements

- Kubernetes, to run Apchi's chart and the Trino it installs. CI runs Apchi's own
  precondition checker against the Trino that chart renders; the requirements themselves are
  §7.1 and §16 of `apchi_implementation.md`, and Apchi names which one failed rather than
  half-applying. Apchi is developed and tested
  against Trino **483**, and several things it relies on were established by reading that
  version rather than its documentation — another version may well work, but no other
  version has been tried. A cluster that authenticates its callers also requires TLS of them
  — that is Trino's rule, not Apchi's — so there the coordinator URL must be `https://`, and
  Apchi refuses an apply it can see would never be answered rather than letting every query
  time out.
- MongoDB, for Apchi's own Snapshots and in-flight state. TLS and `MONGODB-X509`
  authentication are supported, and x509 is worth preferring where it is on offer — the
  connection URI is a Helm value, so an x509 one carries no secret where a password would.
- **Apchi runs inside the cluster**, in the Trino namespace. Validation starts a throwaway
  coordinator and talks to it by pod IP, which is not reachable from a laptop; running Apchi
  outside the cluster makes every validation time out.

## Learning it

[`docs/getting-started.md`](docs/getting-started.md) takes one catalog through the whole loop
— stage, review, validate, apply, read the Snapshot, roll back — with every command and
response from a real run. About ten minutes.

[`docs/concepts.md`](docs/concepts.md) explains the loop, the vocabulary and who owns what.

With Apchi running, `/docs` serves the interactive API reference. The same contract is
published at
[the documentation site](https://infinityhub123.github.io/apchi/reference/api/), and
`openapi.json` in this repository is the committed source of both.

## Where things are

```
app/
  pipeline/   review, validate, apply, verify, commit, auto rollback
  sections/   the six sections above, plus the Admin's raw files
  adapters/   kubernetes, trino, mongo
  auth/       identities, passwords, sessions, tokens, OIDC
  audit/      who changed what, and when
  api/        the REST surface
charts/
  apchi/      Apchi, the Trino it configures, and the Secrets it writes configuration into
deploy/
  trino-dev/  the same Trino as raw manifests, rendered from the chart, for the tests
docs/
  concepts.md             the loop, the vocabulary, and who owns what
  getting-started.md      one catalog through the whole loop, from a real run
  adr/                    the decisions, and what was tried before them
tests/
  tier1/      the HTTP API against real MongoDB and real Trino, Kubernetes faked
  tier2/      the same API against a real cluster
scripts/
  quickstart.sh  the whole thing on a local cluster, one command
```

`apchi_implementation.md` is the design and the reasoning behind every choice in it.
`CONTEXT.md` is the glossary; its terms are the identifiers in the code.

## Current state

Apchi is under active development. Releases publish the image and the chart to GHCR, and
`main` is usually ahead of the latest.

**If you automate against the API**, read this: while Apchi is `0.x` a breaking change may
land in any minor release, and each one is named under `## Breaking` in
[`CHANGELOG.md`](https://github.com/InfinityHub123/apchi/blob/main/CHANGELOG.md). The contract
firms up at 1.0. There is no `/api/v2` planned and no deprecation window — §19 argues why —
and `openapi.json` in this repository is the contract itself.

The backend is complete. The configuration loop is tested end to end against a real Trino on a
real Kubernetes for all six Sections, on both of the deployment shapes Trino clusters come in
(§16).

Built, and covered end to end:

- **Authentication and roles**, but **off by default**. `auth.mode` defaults to `none`, which
  treats every caller as an Admin — so on a default install anything that can reach the pod
  can disable permission enforcement across the cluster. Turn it on: `password` (Argon2id,
  sessions in MongoDB so logout takes effect immediately) or `sso` (OIDC authorization code
  with PKCE, every endpoint discovered from the issuer), plus opaque revocable API tokens.
  Every route carries a gate, and a test walks the published contract so a route added later
  cannot ship ungated. Operators own all six Sections; Admins additionally write Raw Files and
  engage Maintenance Mode.
- **TLS on everything Apchi connects to.** HTTPS to the coordinator, with a client
  certificate where Trino authenticates by one; TLS and x509 to MongoDB; and one CA bundle
  for all four outbound destinations, added to the public store rather than substituted for
  it. Covered against a real Trino 483 with certificate authentication, a real MongoDB
  requiring TLS, and real HTTPS servers for the provider and the webhook.
- **An audit trail.** Who changed what, and when, recorded at the one chokepoint an edit
  cannot skip — so a shared Candidate is still attributable. Refusals are recorded too.
- **Trino's own files.** Apchi renders `config.properties`, `node.properties`,
  `log.properties` and the rest of each role's files under `/etc/trino`: wiring and
  sizing from the chart, every other property from the Admin, validated by Trino on the probe
  first (ADR-0010). The workers' files too, and a change to theirs alone restarts the
  workers, which drain first, and not the coordinator.
- **Admin raw files.** An Admin writes a path and its content and Apchi delivers it, with no
  schema over it.

Not built:

- **No web UI.** The REST API is the whole interface. `/docs` serves an interactive reference.
- **No API change policy.** Breaking changes, deprecated endpoints and fields, and new API
  versions are undecided (§19, §27).
- **Keeping MongoDB is yours to do.** Apchi's Snapshots live there, and operating that
  database is your platform's job — the same division Apchi draws for the Trino deployment
  it configures. Point `mongodb.uri` at something durable; the MongoDB the chart can deploy
  is for trying Apchi out and loses everything when its pod restarts.
- **Certificate rotation needs a restart**, by decision rather than by omission. Everything
  Apchi presents or trusts is read once at startup, which is what keeps one coordinator
  reached through two HTTP libraries from presenting two identities mid-rotation. Apchi logs
  a warning naming the file when a mounted certificate changes under it, and logs each
  certificate's expiry as it loads. Fine for the lifetimes these certificates usually have;
  an issuer renewing hourly would want a reload Apchi does not do (§18, §27).
- **The interactive reference is open, and served from the image.** `/docs`, `/redoc` and
  `/openapi.json` answer without credentials and load nothing from the internet, so they work
  on an air-gapped network. Open by decision: what they expose is the shape of the API, which this repository
  publishes anyway, and "Try it out" sends real requests that meet the gate like any other.

## Licence

Apache 2.0. See [`LICENSE`](https://github.com/InfinityHub123/apchi/blob/main/LICENSE).

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

Nothing about Trino's deployment belongs to Apchi. The Admin owns the Deployment, the heap
sizes and the image; Apchi owns six files inside it, delivered through Secrets the
coordinator mounts. That is the whole coupling, and it is why installing Apchi is just
installing Apchi — there is no sidecar, no operator and no webhook.

Which means the one thing that has to agree is **names**: the Secrets Apchi writes must be
the Secrets the coordinator mounts. The chart's `secrets.*` values are those names, and
`deploy/trino-dev/` is a Trino that mounts exactly them.

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

The chart and the image are published to GHCR on every release:

```sh
helm install apchi oci://ghcr.io/infinityhub123/charts/apchi --version 0.1.0 \
  --namespace trino \
  --set mongodb.uri=mongodb://your-mongo:27017
```

Install it in the namespace Trino runs in — Apchi reads and writes exactly one namespace, and
a validation reaches its probe pod by pod IP. The chart defaults to the image it was released
with, for `linux/amd64` and `linux/arm64`.

Apchi's chart also creates the six Secrets Trino mounts its configuration from, because those
are Apchi's data — so **install Apchi before Trino**. Trino reads two of them while loading
and refuses to boot when either is missing; Apchi starts fine against a Trino that is not
there yet.

If you do not already run Trino on Kubernetes, `charts/trino` is one Apchi can configure:

```sh
helm install trino charts/trino -n trino --set fullnameOverride=trino
```

It is not a general-purpose Trino chart — the official `trino/trino` is that — but it makes
the things Apchi requires impossible to get wrong, and CI proves it by running Apchi's own
preconditions against what it renders. `charts/trino/README.md` says what it guarantees and
what it deliberately leaves out.

`charts/apchi/README.md` documents every value; the ones you are most likely to change are the
Trino Deployment names, the six Secret names and your MongoDB. To install from a clone instead,
`helm install apchi charts/apchi` with the same values.

To try it on minikube or kind with nothing else set up, one command brings up a Trino, an
Apchi and a MongoDB and tells you what to do next:

```sh
./scripts/quickstart.sh
```

## Requirements

- A Trino cluster on Kubernetes, deployed so Apchi can configure it. `charts/trino` is one,
  and CI proves it by running Apchi's own precondition checker against what that chart
  renders. The requirements themselves are §7.1 and §16 of `apchi_implementation.md`, and
  Apchi names which one failed rather than half-applying. Apchi is developed and tested
  against Trino **483**, and several things it relies on were established by reading that
  version rather than its documentation — another version may well work, but no other
  version has been tried.
- MongoDB, for Apchi's own Snapshots and in-flight state.
- **Apchi runs inside the cluster**, in the Trino namespace. Validation starts a throwaway
  coordinator and talks to it by pod IP, which is not reachable from a laptop; running Apchi
  outside the cluster makes every validation time out.

## Learning it

[`docs/getting-started.md`](docs/getting-started.md) takes one catalog through the whole loop
— stage, review, validate, apply, read the Snapshot, roll back — with every command and
response from a real run. About ten minutes.

[`docs/concepts.md`](docs/concepts.md) explains the loop, the vocabulary and who owns what.

With Apchi running, `/docs` serves the interactive API reference and `openapi.json` in this
repository is the committed contract.

## Where things are

```
app/
  pipeline/   review, validate, apply, verify, commit, auto rollback
  sections/   the six sections above
  adapters/   kubernetes, trino, mongo
  api/        the REST surface
charts/
  apchi/      Apchi, and the Secrets it writes configuration into
  trino/      a Trino that Apchi can configure, and that creates no Secrets of its own
deploy/
  trino-dev/  the same Trino as raw manifests, for the end-to-end tests
docs/
  adr/        the decisions, and what was tried before them
scripts/
  quickstart.sh  the whole thing on a local cluster, one command
```

`apchi_implementation.md` is the design and the reasoning behind every choice in it.
`CONTEXT.md` is the glossary; its terms are the identifiers in the code.

## Current state

Apchi is under active development. The configuration loop above is complete and tested
end to end against a real Trino on a real Kubernetes, for all six sections. Not yet built:

- **No authentication.** Every endpoint is open, including the Admin ones. Deploy Apchi
  where only platform staff can reach it.
- **No adoption.** Apchi manages what Apchi configured. A cluster with existing catalogs
  cannot yet import them, and because the coordinator reseeds its catalog store from a Secret
  Apchi owns, a catalog Apchi does not hold disappears at the next coordinator restart.
- **No web UI.** The REST API is the whole interface.

## Licence

Apache 2.0. See [`LICENSE`](LICENSE).

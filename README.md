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

## Requirements

- A Trino cluster on Kubernetes, deployed so Apchi can configure it.
  `deploy/trino-dev/` is an executable reference for what that means; the requirements
  themselves are §7.1 and §16 of `apchi_implementation.md`. Apchi is developed and tested
  against Trino **483**, and several things it relies on were established by reading that
  version rather than its documentation — another version may well work, but no other
  version has been tried.
- MongoDB, for Apchi's own Snapshots and in-flight state.
- **Apchi runs inside the cluster**, in the Trino namespace. Validation starts a throwaway
  coordinator and talks to it by pod IP, which is not reachable from a laptop; running Apchi
  outside the cluster makes every validation time out.

## Getting started

[`docs/getting-started.md`](docs/getting-started.md) brings up Trino and Apchi on minikube or
kind and takes one catalog through the whole loop. It takes about ten minutes.

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
deploy/
  trino-dev/  a Trino that satisfies Apchi's requirements, for development
  apchi-dev/  Apchi and a MongoDB, for development
docs/
  adr/        the decisions, and what was tried before them
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

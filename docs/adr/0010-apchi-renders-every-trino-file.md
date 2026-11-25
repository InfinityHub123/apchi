---
status: proposed
---

# Apchi renders every Trino file; the chart renders none

Apchi writes every file under `/etc/trino`: `config.properties`, `jvm.config`,
`node.properties` and `log.properties` for the coordinator and the workers, alongside the
Sections' files. The chart owns the Kubernetes objects: the image, the worker count,
resources, probes, scheduling, mounts, the network, the Services and Routes. It also owns
**sizing** (the heap, the JVM flags and query memory), because sizing has to fit the memory
limit the chart sets. Sizing reaches Trino as environment variables on the pod, which the
files Apchi renders refer to. The chart mounts Apchi's Secrets and never renders their content.

The boundary used to be drawn by judgement: "infrastructure" belonged to the chart and
"configuration" to Apchi. Every Trino file the chart rendered was a file Apchi could collide
with. On kind, a Raw File at `/etc/trino/config.properties` *replaced* the chart's mount,
because Kubernetes merges container mounts by `mountPath`. A re-apply of the manifests, which
is what Argo CD's sync does, then silently put the chart's mount back. A boundary drawn by who
renders the file can be checked by a precondition. A boundary drawn by judgement cannot be
checked at all.

## Considered

- **Split by how often a setting changes.** Rare settings would go in the chart and frequent
  ones in Apchi. Trino reads one `config.properties`, with no include, so one file would have
  two owners.
- **Sizing in Apchi.** The heap and the memory limit would live in two places. Apchi would
  need its own heap-against-limit check, and a chart change lowering the limit would break the
  Cluster with no Apply to roll back.
- **The chart renders `jvm.config`.** That keeps sizing with the limit, but gives `/etc/trino`
  two writers again. Environment variables keep sizing on the pod template, which rolls with
  the limit, and keep the files single-owner.
- **A Section, recorded in Snapshots.** The Admin's properties are platform decisions. A
  Section Revert would undo them, and Operators could edit them. They are Admin values
  (invariant 9).
- **The chart renders a first copy at install.** That means writing Apchi's generators a
  second time as Helm templates, and the two copies drift. Instead, Apchi writes missing
  Secrets at startup, and the Trino pods wait for them.

## Consequences

- **Wiring** is rendered by Apchi from the chart's settings and refused as Admin input. It
  covers ports, paths, Services, the sizing references such as
  `query.max-memory-per-node=${ENV:TRINO_QUERY_MAX_MEMORY_PER_NODE}`, and all of `jvm.config`.
  "Admin wins on conflict" (§14) does not extend to wiring.
- **`jvm.config` holds no heap flag.** Java places `JDK_JAVA_OPTIONS` before the command line,
  so a heap flag in `jvm.config` would override the chart's (measured). Admins set no JVM
  flags in Apchi.
- **Every sizing variable is required** in the chart. A reference to an unset variable stops
  Trino (measured).
- Apchi checks the Admin's input only where a failure would be invisible: wiring, line breaks,
  `security.refresh-period`. Everything else is judged by Trino on the probe, then by
  Verification and Auto Rollback.
- Apply restarts workers when their files change, so worker graceful shutdown becomes part of
  Apply.
- **Raw Files** may no longer take the six paths Apchi renders. Anywhere else they work as
  before, and survive a re-apply of the manifests (measured).
- **A ninth precondition:** nothing under `/etc/trino` may be mounted from a volume Apchi does
  not write.
- The chart loses `trino.additionalCoordinatorProperties`, `trino.additionalWorkerProperties`,
  `trino.jvm.*` and `trino.securityRefreshPeriod`. Sizing moves to per-role values.

Design, with every measurement: §14, *Trino's server configuration*.

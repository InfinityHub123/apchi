---
status: proposed
---

# Apchi renders every Trino file; the chart renders none

Apchi writes every file under `/etc/trino`, including `config.properties`, `jvm.config`,
`node.properties` and `log.properties` for both the coordinator and the workers. The chart
owns only Kubernetes objects: the image, the worker count, resources, probes, scheduling,
mounts, the network, and the Services and Routes. The chart mounts Apchi's Secrets and never
renders any of their content.

The boundary used to be drawn by judgement: "infrastructure" belonged to the chart and
"configuration" to Apchi. Every Trino file the chart rendered was a file Apchi could collide
with. A Raw File at `/etc/trino/config.properties` replaced the chart's mount without a word,
because Kubernetes merges container mounts by `mountPath`. Once Argo CD put the mount back,
the next Apply replaced it again. A boundary drawn by who renders the file can be checked
by a precondition. A boundary drawn by how often something changes can be checked by
nothing.

## Considered

- **Split by how often a setting changes.** Rare settings (heap, memory) would go in the
  chart, frequent ones in Apchi. Trino reads one `config.properties` and has no include, so
  one file would have two owners. Settings that must agree, such as `-Xmx` and
  `query.max-memory-per-node`, would end up on opposite sides.
- **A Section, recorded in Snapshots.** Heap sizes and log levels are platform decisions, not
  Operator configuration. Recording them in a Snapshot would let a Section Revert undo a
  platform change, and would let an Operator change the heap. They are Admin values
  (invariant 9): applied by an Admin Apply, rolled back from its pre-image.
- **The chart renders a first copy at install, and Apchi takes over.** That means writing
  Apchi's generators a second time as Helm templates, and the two copies drift. Instead,
  Apchi writes the files when they are missing, and Trino waits for the Secret.

## Consequences

- **Wiring is derived, not edited.** These properties name a port, a path or a Service the
  chart creates: the HTTP and HTTPS ports, `discovery.uri`, the keystore path,
  `node.data-dir`, the catalog store, the user-mapping file and the truststore. The chart
  passes them to Apchi as settings, from the same values that create the objects. Apchi
  renders them and refuses them as Admin input, naming the chart value that sets each one.
  "Admin wins on conflict" (§14) does not extend to wiring.
- Apply restarts workers when their files change, so worker graceful shutdown becomes part
  of Apply and not only of node drains.
- Validation checks the heap against the container's live memory limit, because the limit
  is the chart's and the heap is Apchi's.
- A ninth precondition: nothing under `/etc/trino` may be mounted from a volume Apchi does not
  write. It replaces the argument about which paths a Raw File may take with a rule checked
  against the pod template.
- The chart loses `trino.additionalCoordinatorProperties`, `trino.additionalWorkerProperties`,
  `trino.jvm.*` and `trino.securityRefreshPeriod`. An install that still sets them fails to
  render and names the Admin value that replaces each one.
- §16's "Apchi asserts preconditions rather than owning manifests" still holds. Apchi owns
  the files, never the Deployments.

Design: §14, *Trino's server configuration*.

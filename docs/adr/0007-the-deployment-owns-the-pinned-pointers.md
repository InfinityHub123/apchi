# On a whole-directory deployment, the deployment owns the two pinned pointers

Apchi supports two Deployment Shapes. Where the coordinator mounts `/etc/trino` file by file,
Apchi mounts the two files Trino pins to that directory itself. Where it mounts the whole of
`/etc/trino` as one volume, the **deployment** provides those two pointers and Apchi fills only
the directories they name.

The two files are `resource-groups.properties`, read as `etc/resource-groups.properties`, and
the pointer for event listeners — either `etc/event-listener.properties` at Trino's own path,
or `event-listener.config-files` in `config.properties`. No property relocates the first, and
the second lives in a file the chart owns.

## The constraint

Kubernetes will not place a single file inside a read-only directory volume. The target does
not exist in that volume, so there is nothing to bind onto, and runc fails the container before
Trino runs — so there is no Trino log to read:

```
error mounting ".../volume-subpaths/extra/c/1" to rootfs at "/etc/trino/user-mapping.json":
  not a directory
-> ContainerCannotRun
```

A nested *directory* mount in the same position works. Both reproduced on a real cluster rather
than inferred.

The official `trinodb/charts` chart mounts its coordinator ConfigMap at the whole of
`/etc/trino`, and so does most hand-written manifest. Apchi's own preconditions refused that
chart outright.

## Considered options

**Keep single-file mounts and declare whole-directory deployments unsupported.** Honest, and
no code. Rejected: it rules out the chart nearly everyone deploys, which makes Apchi a tool for
clusters that were set up to suit it.

**Have Apchi write into the chart's own ConfigMap.** Then nothing about the mounts changes.
Rejected: that ConfigMap is Helm-managed, so the next `helm upgrade` reverts Apchi's writes,
and §16's whole position is that Apchi asserts preconditions rather than owning the Admin's
manifests.

**Move every file into a directory Apchi mounts.** Works for four of the six Sections and is
what #93 did. It cannot work for these two, because the paths are Trino's and not Apchi's.

**The deployment provides the pointer, as a per-Section mode (selected).** The division already
in force for `access-control.properties`, which Apchi has never written: the Admin says where
the configuration is, Apchi fills it. Both modes are off by default, because the shape that
needs them is the one Apchi cannot detect without guessing — and a deployment it cannot read
tells it nothing.

## Consequences

- **Section ownership does not change.** Operators still own all six Sections. A pointer names
  where a file lives and never varies with the Candidate; what is *in* the file stays the
  Operator's. Nobody calls Permissions an Admin Section for the same reason.

- **A precondition has to check the pointer is really there**, and really names the directory
  Apchi fills. An Admin who turns the mode on and forgets the property gets a Section that
  writes files nobody reads: the Apply succeeds, the Secret is correct, and the Cluster behaves
  as if the Section were empty. Read from the Admin's own configuration, the way the
  `security.refresh-period` check is.

- **Event Listeners cannot be empty on such a deployment.** The pointer always names a file and
  Trino refuses to start without the one it names, so emptying is refused — at request time,
  because the Candidate is shared and an empty Section caught only by Validation blocks
  everyone's Apply (ADR-0002), and again during Validation for the paths that do not go through
  a delete. Trino ships no listener that does nothing, so there is nothing honest to render in
  place of none.

  The refusal is on the Section's **state**, not the transition into it. Scoping it to the
  transition looks kinder and is unsafe: `files.by_secret` writes each Secret whole, so an
  Apply with the Section empty writes an empty Secret, the mounted directory loses the file the
  pointer names, and the next restart leaves a coordinator that cannot come back — with Apchi
  having deleted the Admin's own seeded file on the way past. This was tried and reverted.

- **Resource Groups can be empty**, because Trino supplies the answer. When
  `etc/resource-groups.properties` is absent, `InternalResourceGroupManager` returns early and
  `LegacyResourceGroupConfigurationManager` stays in force: its `match()` returns
  `ResourceGroupId("global")` for every query, and `configure()` takes
  `query.max-concurrent-queries` (default 1000) and `query.max-queued-queries` (default 5000).
  So an empty Section renders exactly that document, catch-all selector included, and both
  Deployment Shapes behave identically.

  The selector is load-bearing. The legacy manager matches queries itself rather than
  consulting selectors, and a document with groups and none rejects every query with *"No
  matching resource group found with the configured selection rules"* — verified against a
  running coordinator.

- **"No resource groups" has never meant "no limits" in Trino.** A Cluster with no rules file
  still runs under 1000 concurrent and 5000 queued. An earlier version of this reasoning
  asserted the opposite, and that Apchi would have to invent a concurrency policy to render a
  fallback. Both were wrong.

- **Admin raw files do not work on a whole-directory deployment at all.** A raw file's path is
  the Admin's to choose, which makes it a single-file mount by nature, so there is no directory
  to move it into without removing the feature. `config.properties` cannot work there even in
  principle, because on such a deployment that file *is* the chart's ConfigMap. The refusal
  names the chart values to use instead.

- **Four Secrets must hold a file before the coordinator first starts.** Trino validates every
  path property at startup and Apchi writes those Secrets on its first Apply, which is after.
  The two Apchi normally leaves empty are the resource groups and event listener ones, because
  in the default mode their absence *is* "none configured".

- **`additionalConfigProperties` reaches workers as well as the coordinator.** Trino validates
  path properties on every node, so the Secrets have to be mounted on both roles.

  The last two were found by installing the official chart, not by rendering it and checking
  the pod template. Rendering proves the template is acceptable; it does not prove the cluster
  starts.

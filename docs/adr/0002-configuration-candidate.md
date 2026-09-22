# A single Configuration Candidate, not per-user transactions

Operators edit one mutable Configuration Candidate per Cluster, derived from the latest
Snapshot. Resource changes (`POST /catalogs`, `PUT /resource-groups`) land in it directly;
`POST /applies` promotes it through the lifecycle. There is no transaction object to create,
reference or delete, and no transaction id in the API.

## Considered options

**Per-Operator transactions with optimistic concurrency**, each recording a parent version
and rejected at Apply if the parent is stale. Gives isolation, but an Operator can edit for
an hour and then be told their work cannot be applied — with no in-product way to have
known earlier. It also requires a lifecycle object, an id on every mutating request, and
conflict-resolution semantics.

**A single shared Candidate (selected).** Simpler API surface and no staleness by
construction, at the cost of isolation: two Operators editing simultaneously share one
Candidate, so an Apply ships both sets of changes.

## Consequences

- Concurrent editing is not isolated. Mitigated by the `review` endpoint, which returns the
  diff between the Candidate and the latest Snapshot and is called by the UI before every
  Apply, and by the audit trail, which records who changed what. An API caller who does not
  call `review` can apply another Operator's staged changes; accepted, on the basis that
  one Operator interacts with the API at a time.
- The Candidate is frozen during Apply — mutations are rejected while an Apply is in
  flight, so what is committed is what was verified. This holds for every Apply engine, not
  only the one that restarts Trino, and for an Admin Apply as well as an Operator one.
- Apply succeeds: the Candidate is re-derived from the new Snapshot. Apply fails: the
  Candidate is preserved intact so the Operator can fix and retry. An explicit reset
  endpoint returns it to the latest Snapshot.

# Apply is not atomic, and orders creates before drops

The file engines are recoverable: each write either lands or it does not, and an Apply that
writes several files is not atomic across them but is always repairable by writing them
again, because the desired state is declarative.

Catalogs are not. They are applied as a sequence of SQL statements (ADR-0001), so an Apply
can fail partway and leave the Cluster matching neither the previous Snapshot nor the
Candidate — and no rewrite repairs it, because the change was imperative.

Apply therefore orders `CREATE` and `ALTER` before `DROP`, so that a failure midway leaves
a superset of both states. An unused catalog breaks nobody; a missing one breaks every
query against it.

On failure, Auto Rollback compensates: drop what this Apply created, restore what it
dropped, in one bounded attempt. If that attempt fails, Apchi stops touching the Cluster,
raises an incident alert and engages Maintenance Mode. It never retries — two consecutive
failures mean the problem is not the configuration.

## Consequences

- "Discarding before Apply leaves production unchanged" still holds, because nothing
  reaches the Cluster before Apply. "Apply is all-or-nothing" does not, and the
  implementation document states this explicitly rather than implying atomicity.
- `DROP CATALOG` permanently deletes the backing `.properties` file, and the Hive, Iceberg,
  Delta Lake and Hudi connectors are documented as not releasing all resources when a
  catalog is dropped. Compensating drops are not free.

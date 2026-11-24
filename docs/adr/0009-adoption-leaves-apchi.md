# Adoption leaves Apchi

Apchi no longer brings a running Trino under management by reading it. The `/admin/adoption`
surface, its discovery, its seed, the file parsers behind them and the high-water mark are
gone. Moving an existing Cluster's configuration into Apchi becomes the job of an internal
automation outside this repository, which reads that Cluster's files and hands Apchi the
result as a configuration document.

Adoption was the largest single part of Apchi that was not the loop. It meant:
- a parser for every Section, the inverse of each generator, kept round-trip exact;
- a reader for every way an Admin might mount a file;
- a five-step procedure with a seed and a cutover;
- a ConfigMap outside MongoDB, so that an empty database could be told apart from a new
  install.

All of that existed to run once per Cluster. The automation that replaces it reads files
from one environment to set up another, which is a migration tool's job and not a control
plane's.

## Considered

- **Keep the parsers for the automation to call.** They are the most reusable part, but
  nothing in Apchi would call them, and code here that nothing here exercises is code that
  drifts. They stay in git history for the automation to take.
- **Keep the high-water mark.** Adoption was the only thing that read it. A number nothing
  reads is not evidence of anything.

## Consequences

- An existing Cluster's configuration enters Apchi through the configuration import that
  already exists: `PUT /api/v1/admin/configuration` takes the six Sections and the Admin
  values, including the preserved access-control rules and certificate mapping patterns that
  Apchi's models cannot express. The first Apply after it produces Snapshot 1, so §9's
  invariant still holds.
- `/api/v1/admin/adoption/*` is gone from the contract.
- `APCHI_HIGH_WATER_CONFIG_MAP` is gone. An install that sets it outside the chart stops at
  startup, naming it, because Apchi refuses settings it does not read.
- The chart's `highWaterConfigMap` value is gone, and Apchi's Role no longer creates or
  patches ConfigMaps. An existing high-water ConfigMap is left behind and can be deleted.
- Losing MongoDB is still the platform's to prevent (§18). What to do after it happens is
  the same answer as for any Cluster with configuration Apchi does not hold: import it.

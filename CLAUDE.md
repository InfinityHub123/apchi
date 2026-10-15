# Apchi

Control plane for configuring one Trino cluster. Design and the reasoning behind every
choice below: `apchi_implementation.md`, §22 (technology) and §23 (conventions).
Vocabulary: `CONTEXT.md`.

## Agent skills

### Issue tracker

GitHub Issues on `InfinityHub123/apchi`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, each label string equal to its name. See
`docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Build order

Backend first, end to end, before any frontend work begins. Slice 1 is catalogs; slice 2 is
event listeners. §24.

## Layout

```
app/
  pipeline/   candidate, review, validate, apply, verify, commit, auto_rollback
  sections/   catalogs, client_certificates, certificate_mapping, permissions,
              resource_groups, event_listeners, and raw_files -- which is the Admin's and
              deliberately not a Section, because it never enters a Snapshot
  adapters/   kubernetes, trino, mongo
  auth/       identity, passwords, users, sessions, tokens, oidc, logins
  audit/      §20's events: who changed what, and when
  api/        routers
```

A Section provides exactly four things: a model, a configuration generator, an apply
strategy, and whether it requires a restart.

## Vocabulary

`CONTEXT.md`'s terms are the identifiers: `Snapshot`, `ConfigurationCandidate`, `Section`,
`apply`, `rollout`, `validate`, `verify`, `commit`, `reset`, `section_revert`,
`full_rollback`, `auto_rollback`, `maintenance_mode`, `TrinoIdentity`,
`CertificateMappingPattern`, `Identity`, `Role`, `RawFile`, `AuditEvent`. Where the glossary
defines a term, use that term and no synonym for it.

Two pairs are easy to conflate, and the glossary separates them deliberately: `Identity` is an
Apchi caller, `TrinoIdentity` is whoever runs a query; a `Section` enters a Snapshot, a
`RawFile` never does.

## Libraries

Reach for these rather than the ones most tutorials suggest:

- **MongoDB** — PyMongo's Async API, optionally via Beanie. Motor is past end-of-life.
- **SSE** — `from fastapi.sse import EventSourceResponse, ServerSentEvent`, built into
  FastAPI. `sse-starlette` is a redundant dependency.
- **Kubernetes** — the official `kubernetes` client.
- **Trino** — the official `trino` package; `httpx` for `/v1/info`, `/v1/status` and
  `/v1/query`, which are not part of Trino's documented API. Cluster membership comes from the
  `system.runtime.nodes` system table, not `/v1/node` — that endpoint 404s on Trino 483.
- **Trino in tests** — `testcontainers.community.trino`.

## Deployment shapes

Trino clusters mount `/etc/trino` in one of two ways, and it decides what Apchi can deliver.

**File-by-file**, which `deploy/trino-dev` uses: each file is its own mount, so Apchi can add
single-file mounts inside it.

**Whole-directory**, which the official `trinodb/charts` chart uses: one ConfigMap at
`/etc/trino`. Kubernetes will not place a single file inside a read-only directory volume --
runc fails the container with `not a directory` and Trino never starts, so there is no log to
read. A nested *directory* mount in the same position is fine, which is why most of what Apchi
writes is a directory the Admin mounts.

Two files Trino pins to that directory cannot move: `resource-groups.properties` is read as
`etc/resource-groups.properties`, and the alternative to `etc/event-listener.properties` is
`event-listener.config-files` in `config.properties`. On a whole-directory deployment the Admin
provides those two pointers and Apchi fills the directories they name; the settings are
`resource_groups_properties_from_deployment` and
`event_listener_config_files_from_deployment`, both off by default. Admin raw files do not work
there at all, because their path is the Admin's to choose. See §16 and
`docs/official-trino-chart.md`.

Rendering a chart and checking the pod template is not the same as installing it. Both errors
in the documented values for the official chart were found by installing it, not by rendering
it: `additionalConfigProperties` reaches workers as well as the coordinator, and four Secrets
must hold a file before the coordinator first starts.

## Tests

Two tiers. Tier 1 drives the HTTP API against real MongoDB and real Trino with Kubernetes
faked; tier 2 runs against a real cluster and is marked `tier2`.

`pytest tests/tier1` runs the whole suite in one process, in a bit over three minutes.

**Tier 2 is sharded positionally**, four ways, by
`ls tests/tier2/test_*.py | awk "NR % 4 == <shard> % 4"`. So **adding a tier 2 file reshuffles
every shard**, and a test that depends on what ran before it starts failing somewhere else.
Before pushing a new tier 2 file, compute its shard the way CI does and run that shard
locally -- which is cheaper than three CI cycles, and is how the last two reshuffles were
caught. A tier 2 test that changes the Cluster deliberately puts it back itself, rather than
leaving the rest of its shard to tolerate it.

It used to hang -- 5519s and 18 timeouts, with no assertion ever failing -- which is what
`scripts/tier1.sh` existed to work around. The cause was the test MongoDB's **open-file
limit**: Docker defaults the soft `nofile` limit to 1024 and MongoDB asks for 64000, so mongod
stopped accepting connections and said so in its own log. The container now sets the limit and
the workaround is gone (#69). If the suite ever hangs on a database that is up and idle, read
`docker logs` on the MongoDB container before anything else.

## Types

Strict type checking in `pipeline/` and `sections/`, relaxed in `adapters/`. Pydantic models
cross every module boundary, and reject unknown fields. Domain strings that could be
confused for one another — a catalog name, a certificate CN, a Section name — get distinct
types.

## Errors

Every `except` names the exception it catches. A failure inside a loop fails the operation:
catching per item and logging yields an operation that reports success having done nothing.

## Async

`run_in_threadpool` lives in `adapters/`, wrapping the synchronous Kubernetes and Trino
clients. Pipeline code and route handlers are plain `async` and never mention threads.

## Comments

Comment the constraint, not the mechanism. Name the Trino or Kubernetes behaviour a reader
could not infer from the code, and the section that argues it:

```python
# kubelet takes up to syncFrequency (60s) to project a ConfigMap change into the
# pod, and Trino then re-reads the rules file on its own security.refresh-period
# timer. The two delays add. See §7.5.
await self._wait_for_rules_reload()
```

## Logging

A `LOG_LEVEL` setting, defaulting to `DEBUG` in np and test and `INFO` in prep and prod. A
`contextvar` carries the id of the Apply in progress into every record, so one id yields the
whole story of a failure. Log resource names and diff summaries (`catalogs: +finance_pg`); a
redaction filter keyed on secret-bearing field names runs at every level in every
environment, because Snapshots hold real credentials.

## OpenAPI

`openapi.json` is committed. CI regenerates it and fails on a diff, so every contract change
arrives as a reviewed diff.

## Commits

A commit message names the behaviour that changed and the reason it changed. Subject line
first; add a body whenever the reason is not obvious from the subject.

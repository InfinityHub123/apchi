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
              resource_groups, event_listeners
  adapters/   kubernetes, trino, mongo
  api/        routers
```

A Section provides exactly four things: a model, a configuration generator, an apply
strategy, and whether it requires a restart.

## Vocabulary

`CONTEXT.md`'s terms are the identifiers: `Snapshot`, `ConfigurationCandidate`, `Section`,
`apply`, `rollout`, `validate`, `verify`, `commit`, `reset`, `section_revert`,
`full_rollback`, `auto_rollback`, `maintenance_mode`, `TrinoIdentity`,
`CertificateMappingPattern`. Where the glossary defines a term, use that term and no
synonym for it.

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

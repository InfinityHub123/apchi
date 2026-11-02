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
  TLS goes in as keyword arguments, not as query parameters on the URI, so that a typo is a
  `ConfigurationError` at startup rather than an option PyMongo silently does not recognise.
  The CA comes from `app.trust`, not from a setting read here.
  `tlsCertificateKeyFile` takes **one** PEM holding the certificate and its key together,
  where the Trino client wants the two apart. Either file option turns TLS on by itself --
  PyMongo builds its SSL context from them and does not also need `tls=true`. PyMongo opens
  them while validating its options and `Mongo` is built in the lifespan, so a missing file
  crashes Apchi rather than producing the 503 an unreachable MongoDB gives.
- **SSE** — `from fastapi.sse import EventSourceResponse, ServerSentEvent`, built into
  FastAPI. `sse-starlette` is a redundant dependency.
- **Kubernetes** — the official `kubernetes` client.
- **Trino** — the official `trino` package; `httpx` for `/v1/info`, `/v1/status` and
  `/v1/query`, which are not part of Trino's documented API. Cluster membership comes from the
  `system.runtime.nodes` system table, not `/v1/node` — that endpoint 404s on Trino 483.
  The coordinator is configured as a **URL**, never a host and a port: the client infers
  `https` from the port at *exactly* 443, so 8443 — Trino's own documented HTTPS port — came
  out as plaintext against a TLS listener and said nothing. It accepts a URL in place of
  `host`, which removes the inference entirely. Trust configuration goes through
  `app.trust`, never per client: see **Outbound TLS** below.
- **Trino in tests** — `testcontainers.community.trino`.

## Outbound TLS

Four destinations — the Trino coordinator, MongoDB, the OIDC provider, the alert webhook —
through three HTTP libraries. **All trust goes through `app/trust.py`; no client reads
`ca_file` itself.**

The reason is one measured fact: a CA *path* handed to PyMongo or to the Trino client's
`requests` session makes them trust that CA **alone** — 1 CA, against 147 for a default
context with the same CA loaded into it. Only httpx takes a context built the additive way.
So `app.trust` concatenates the public store with the configured CA once and hands every
client the result, which keeps one meaning of "trust this CA" across all four.

Without it, an internal OIDC provider would stop a public alert webhook verifying, and
Trino — one destination reached through two libraries — would have its liveness check and its
queries disagree. `requests` also reads `REQUESTS_CA_BUNDLE` while httpx reads
`SSL_CERT_FILE`, so none of this is left to the environment. httpx additionally deprecates
both a path-valued `verify` and `cert` in favour of an `ssl.SSLContext`.

**Read once, at startup, and never again** — and the certificate Apchi *presents* is a copy
`app.trust` owns, not the mounted file. Same reason, pointing the other way: `requests` is
handed the two paths and reopens them at every handshake, where httpx holds the bytes, so
against a rotating Secret the query session and the liveness check presented two different
identities. Reloading was rejected: PyMongo keeps the context it builds with the client, so
MongoDB cannot follow without being rebuilt, and a CA reloaded for Trino alone splits trust
across destinations. So a rotation needs a pod restart — logged with the file's name, and
never a refusal, because what Apchi loaded still works until the destination stops accepting
it. A certificate's subject and expiry are logged as it loads, and the Apply path warns again
when one is inside `certificate_expiring_within_days` — of what was *loaded*, never of what is
mounted, since a rotation makes those two different certificates. An Apply rather than a timer
because there is no periodic task in this codebase, which leaves a Cluster nobody applies to
hearing nothing between restarts — accepted, with a log alert on `app.trust` as the answer
rather than a scheduler (#196). See §18, #193.

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

## Docs site

`mkdocs.yml` at the root, Material, built by CI with `--strict` and published to GitHub Pages
from `main` by `.github/workflows/docs.yml`. `--strict` fails on a link whose target moved and
on a doc file that is in the tree but in no `nav` entry -- the second being how a docs site
rots, since the page still exists and nothing points at it.

**The prose does not move to serve the site.** `README.md`, `CONTEXT.md` and
`apchi_implementation.md` stay at the root because this file and `docs/agents/domain.md` send
agents to those exact paths, and each chart's README belongs beside its chart. The site
includes them from where they are and `include-markdown` rewrites their relative links, which
means a link between two of them has to resolve *both* in the repo and on the site -- why the
spec names `CONTEXT.md` in backticks instead of linking to it. `openapi.json` is copied in by
`scripts/mkdocs_hooks.py`, in `on_config` because hooks run after plugins, rather than
duplicated under `docs/`. `docs/agents/` is excluded: it is instructions to a tool.

## Commits

A commit message names the behaviour that changed and the reason it changed. Subject line
first; add a body whenever the reason is not obvious from the subject.

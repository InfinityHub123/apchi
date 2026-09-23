# Coordinator rollout uses RollingUpdate, and a rollout destroys running queries

Trino cannot drain a coordinator: `NodeStateManager.transitionState()` throws
`UnsupportedOperationException("Cannot drain coordinator")`, and the graceful shutdown API
is documented as usable "exclusively on workers". There is no coordinator HA
(trinodb/trino#391, open since 2019). An Apply that restarts the coordinator therefore
terminates every running and queued query, and no configuration of Apchi changes that. Only
the rollout engine does this: catalogs, client certificates and permissions apply without a
restart.

The coordinator Deployment uses `RollingUpdate` behind a single Service. Draining will be
solved later by putting Trino Gateway in front of two full clusters and cutting traffic
over blue/green.

## Considered options

**`Recreate`.** All queries die at once, deterministically, in one ~30-60s window.

**`RollingUpdate` behind a single Service (selected).** Because the coordinator runs at
`replicas: 1`, Kubernetes defaults compute to `maxUnavailable: 0, maxSurge: 1`, so the new
coordinator starts before the old one is removed and both are in the Service endpoints for
the overlap. Two consequences follow, and are accepted:

- `nextUri` is rebuilt from the incoming request's base URI
  (`Query.createNextResultsUri()` via `ExternalUriInfo.baseUriBuilder()`), so client polls
  re-enter the Service and may land on the coordinator that did not start the query. Query
  state is a per-process `ConcurrentMap<QueryId, Query>`, so that returns HTTP 404
  "Query not found", which clients treat as fatal.
- Workers announce to `discovery.uri` every 5s and each coordinator's `AnnounceNodeInventory`
  is an in-process cache with a 30s TTL, so during the overlap each coordinator sees a
  partial, shifting subset of workers.

Neither strategy preserves a single query. `Recreate` fails them cleanly and briefly;
`RollingUpdate` fails a subset of them with a misleading error over a longer window. The
choice was made deliberately, with these consequences understood, on the basis that
draining is being deferred to Gateway blue/green rather than solved here.

**Access-control drain** (deny `execute` via hot-reloaded rules, poll `/v1/query` to zero,
then restart) was designed and deferred. It would achieve zero query loss without a
gateway, at the cost of a rejection window. No public write-up of this technique exists;
it would be novel.

## Consequences

- The Apply confirmation must state that running queries will be terminated, with a live
  query count from the coordinator.
- `RollingUpdate` becomes genuinely useful once Gateway blue/green exists, because Gateway
  routes by query id and can drain a cluster before it is replaced.

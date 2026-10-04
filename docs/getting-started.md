# Getting started

This brings up a Trino cluster and an Apchi on your own machine and takes one catalog
through the whole configuration loop. Every command and every response below was run
against minikube with Trino 483; nothing here is illustrative.

Expect about ten minutes, most of it waiting for Trino to start.

## What you need

- `kubectl`, and a cluster to point it at. minikube or kind is fine; this walkthrough uses
  minikube.
- About 4 GB of memory free for the cluster. Trino's coordinator, one worker and the
  throwaway coordinator a validation starts are all real JVMs.
- `curl` and `python3`, to read the JSON.

Everything lands in the `default` namespace. Apchi reads and writes one namespace — the
cluster's own — and nothing outside it.

## 1. A Trino to configure

Apchi does not install Trino. It configures a Trino that is already deployed, and expects
that deployment to satisfy a handful of requirements: a catalog store it can write, the
generated files mounted where Apchi will put them, and an access control file with a refresh
period on it. `deploy/trino-dev/` is a Trino that satisfies all of them, small enough to run
on a laptop.

```sh
kubectl apply -f deploy/trino-dev/
kubectl wait --for=condition=ready pod -l app=trino --timeout=300s
```

`deploy/trino-dev/README.md` explains what each piece is for and what testing it on a real
cluster corrected. If you already run Trino on Kubernetes, read §7.1 and §16 of
`apchi_implementation.md` before pointing Apchi at it: Apchi refuses to apply against a
deployment that does not satisfy the preconditions, and tells you which one failed.

## 2. Apchi

Apchi has to run **inside** the cluster. A validation starts a throwaway Trino coordinator
and talks to it by pod IP, so an Apchi on your laptop reaches Kubernetes fine and then times
out every validation.

Build the image into your cluster's daemon and deploy it:

```sh
minikube image build -t apchi:dev .
kubectl apply -f deploy/apchi-dev/
kubectl rollout status deploy/apchi --timeout=300s
```

On kind, build with Docker and load the result instead of the first line:

```sh
docker build -t apchi:dev . && kind load docker-image apchi:dev
```

That gives you Apchi, a MongoDB for its Snapshots, and the RBAC Role listing exactly which
Kubernetes verbs Apchi uses. Reach the API through a port-forward:

```sh
kubectl port-forward svc/apchi 8000:8000 &
curl -s localhost:8000/api/v1/health
```

```json
{"status":"ok","mongo":"up"}
```

`localhost:8000/docs` is the interactive API reference, generated from the same contract as
the committed `openapi.json`.

For the rest of this page:

```sh
A=http://localhost:8000/api/v1
```

## 3. Stage a catalog

Nothing you do now reaches Trino. Every edit lands in the **Configuration Candidate**, the
one mutable configuration Apchi holds, and stays there until you apply.

```sh
curl -s -X POST $A/catalogs -H 'content-type: application/json' \
  -d '{"name":"sales","connector":"tpch","properties":{"tpch.splits-per-node":"4"}}'
```

```json
{"name":"sales","connector":"tpch","properties":{"tpch.splits-per-node":"4"},
 "certificate":null,"supported":false}
```

`supported: false` is not a problem — it says Apchi has no curated schema for the `tpch`
connector, so its properties went through unchecked and only Trino will judge them.
For the connectors Apchi does curate — `postgresql`, `hive`, `iceberg`, `mongodb`, `kafka`,
`elasticsearch`, `redis` — a typo is refused at the request:

```sh
curl -s -X POST $A/catalogs -H 'content-type: application/json' \
  -d '{"name":"finance","connector":"postgresql","properties":{"connection-uri":"jdbc:postgresql://db:5432/f","connection-user":"t"}}'
```

```json
{"code":"unprocessable_payload",
 "message":"The properties are not valid for the 'postgresql' connector.",
 "details":[
   {"property":"connection-uri","problem":"'connection-uri' is not a property of the 'postgresql' connector; did you mean 'connection-url'?"},
   {"property":"connection-url","problem":"'connection-url' is required by the 'postgresql' connector"}],
 "request_id":"3456810a19264a48b09ba5a3a743f94d"}
```

## 4. Review what an apply would do

```sh
curl -s $A/review | python3 -m json.tool
```

```json
{
  "base_snapshot": null,
  "has_changes": true,
  "sections": [
    {"section": "catalogs",
     "changes": [{"resource": "sales", "change": "added", "before": null,
                  "after": {"connector": "tpch", "properties": {"tpch.splits-per-node": "4"}}}]},
    {"section": "client_certificates", "changes": []},
    {"section": "certificate_mapping", "changes": []},
    {"section": "event_listeners", "changes": []},
    {"section": "permissions", "changes": []},
    {"section": "resource_groups", "changes": []}
  ],
  "cost": {"restarts_coordinator": false, "queries_at_risk": null, "warning": null}
}
```

Three things worth reading there.

`base_snapshot` is `null` because nothing has been applied yet. Review is a diff against the
Snapshot the Candidate came from, not a dump of what is staged.

Every section is listed, not only the one you touched. The Candidate is shared, and an apply
promotes all of it — so review is where you find out somebody else staged something.

`cost` is what this apply does to the running cluster. A catalog costs nothing. Later in this
walkthrough you will see the other answer.

## 5. Validate

Validation decides whether the Candidate *should* be applied, before the cluster is touched
at all. Where a section's configuration is something a coordinator could refuse, Apchi starts
a throwaway coordinator, feeds it the generated files, and asks it.

```sh
curl -s -X POST $A/validations
```

```json
{"id":"val_51feb6583e064d6f","outcome":"running","failures":[],
 "base_snapshot":null,"started_at":"2026-10-04T07:42:16.672797Z","finished_at":null}
```

It runs in the background; poll it:

```sh
curl -s $A/validations/val_51feb6583e064d6f
```

```json
{"id":"val_51feb6583e064d6f","outcome":"passed","failures":[],"base_snapshot":null,
 "started_at":"2026-10-04T07:42:16.672797Z","finished_at":"2026-10-04T07:42:24.632677Z"}
```

Eight seconds, including starting a Trino, issuing the `CREATE CATALOG` against it and
deleting the pod. A validation that fails names the resource responsible rather than handing
you a coordinator log.

Validating separately is optional — an apply validates first and refuses to go further if it
fails. It is there so you can check a Candidate without committing to applying it.

## 6. Apply

```sh
curl -s -X POST $A/applies
```

```json
{"id":"apl_7526623bbb2f4f96","stage":"validating","history":[...],"snapshot":null, ...}
```

Follow it as it happens:

```sh
curl -sN $A/applies/apl_7526623bbb2f4f96/events
```

```
event: stage
data: {"stage": "validating", "at": "2026-10-04T07:42:40.449625+00:00", "detail": null, "failure_reason": null}
id: 0

event: stage
data: {"stage": "applying", "at": "2026-10-04T07:42:47.558723+00:00", "detail": null, "failure_reason": null}
id: 1

event: stage
data: {"stage": "verifying", "at": "2026-10-04T07:42:47.828414+00:00", "detail": null, "failure_reason": null}
id: 2

event: stage
data: {"stage": "committing", "at": "2026-10-04T07:42:48.493855+00:00", "detail": null, "failure_reason": null}
id: 3

event: stage
data: {"stage": "succeeded", "at": "2026-10-04T07:42:48.502616+00:00", "detail": null, "failure_reason": null}
```

Eight seconds, and `GET $A/applies/apl_7526623bbb2f4f96` now reports `"snapshot": 1`. The
stream ends when the apply does. There are three ways it can end: `succeeded`, `failed` —
the apply did not go through and Apchi put the cluster back — and `incident`, which means the
auto rollback failed too, Apchi has stopped touching the cluster, and maintenance mode is
engaged until an admin clears it.

**Verifying** is not a formality. Trino exposes no endpoint saying which configuration is
live, so verification asks functionally: is the coordinator answering, have the workers
registered, does a real query run, and does each section agree the cluster adopted it. If any
of that fails, Apchi puts the cluster back to the last Snapshot by itself and the apply fails.

The catalog is live in Trino:

```sh
kubectl exec deploy/trino-coordinator -c trino -- trino --execute "SHOW CATALOGS"
```

```
"sales"
"system"
"tpch"
```

`tpch` is the catalog `deploy/trino-dev/` seeds, and it is still there — the apply added
`sales` and took nothing away. But Apchi has no adoption yet, so `tpch` is a catalog Apchi
does not know about, and that has a consequence worth understanding before you point Apchi at
a cluster you care about: the durable copy of every catalog is a Secret Apchi owns and
rewrites, and the coordinator reseeds its catalog store from that Secret at every start. So a
catalog Apchi does not hold survives until the next coordinator restart and then disappears.
Run `SHOW CATALOGS` again after step 8, which restarts the coordinator, and `tpch` is gone.

Until adoption exists, stage everything you want to keep before the first restart.

## 7. The Snapshot

```sh
curl -s $A/snapshots | python3 -m json.tool
```

```json
[
  {
    "number": 1,
    "sections": {
      "catalogs": {"sales": {"connector": "tpch", "properties": {"tpch.splits-per-node": "4"}}},
      "client_certificates": {}, "certificate_mapping": {}, "event_listeners": {},
      "permissions": {}, "resource_groups": {}
    },
    "created_at": "2026-10-04T07:42:48.495229Z",
    "apply_id": "apl_7526623bbb2f4f96"
  }
]
```

A Snapshot is the whole configuration, not a diff, and it is immutable. It is what rollback
rolls back to and what an auto rollback restores.

## 8. A change that costs something

Half the sections reach Trino without a restart. The other half Trino adopts only by
restarting, and Apchi says so before you commit. Add a resource group and a selector that
sends every query to it:

```sh
curl -s -X POST $A/resource-groups -H 'content-type: application/json' \
  -d '{"path":"adhoc","hard_concurrency_limit":10,"max_queued":100,"soft_memory_limit":"40%"}'
curl -s -X PUT $A/resource-groups/selectors -H 'content-type: application/json' \
  -d '{"selectors":[{"group":"adhoc"}]}'
curl -s $A/review | python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin)["cost"], indent=2))'
```

```json
{
  "restarts_coordinator": true,
  "queries_at_risk": 0,
  "warning": "Applying this restarts the Trino coordinator, which terminates every running and queued query. Trino cannot drain a coordinator, so there is no way to avoid it."
}
```

`queries_at_risk` is counted from the cluster at the moment you asked, so on a busy cluster
it is a real number and the decision is yours. Apply it the same way; the stage list now has
`rolling_out` in it, and the whole thing takes about 45 seconds:

```
validating → applying → rolling_out → verifying → committing → succeeded
```

## 9. Roll back

Snapshot 2 has the resource group. To go back to Snapshot 1, stage it in its entirety and
apply:

```sh
curl -s -X POST $A/candidate/rollback -H 'content-type: application/json' -d '{"snapshot":1}'
curl -s $A/review | python3 -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps({"base_snapshot": d["base_snapshot"], "cost": d["cost"]}, indent=2))'
```

```json
{
  "base_snapshot": 2,
  "cost": {
    "restarts_coordinator": true,
    "queries_at_risk": 0,
    "warning": "Applying this restarts the Trino coordinator, which terminates every running and queued query. Trino cannot drain a coordinator, so there is no way to avoid it."
  }
}
```

Rollback is an ordinary apply: it validates, it verifies, and it costs whatever undoing the
change costs. Once it succeeds the snapshot list reads `[3, 2, 1]` — rolling back to
Snapshot 1 produced Snapshot **3**. History is never rewritten, so a rollback is itself
something you can roll back.

To undo one section rather than everything, every section has a revert — and it takes the
same body, so the two recovery actions cannot drift apart:

```sh
curl -s -X POST $A/catalogs/revert -H 'content-type: application/json' -d '{"snapshot":1}'
```

That stages the catalogs from Snapshot 1 and leaves the other five sections exactly as they
are. It is the routine recovery: one catalog broke, the rest is nobody's business. To throw
away what you staged without applying anything at all, `POST $A/candidate/reset`.

## 10. Permissions, and why your grant changes nothing yet

```sh
curl -s -X POST $A/permissions -H 'content-type: application/json' \
  -d '{"identity":"analyst","catalog":"sales","privileges":["SELECT"]}'
```

```json
{"identity":"analyst","catalog":"sales","schema":null,"table":null,
 "privileges":["SELECT"],"key":"analyst:sales:*:*"}
```

That applies with no restart — the access control file has a refresh period on it and Trino
re-reads it on its own timer. But it does not yet restrict anybody. Apchi ships a catch-all
rule allowing everything no grant mentions, because writing grants on a cluster already
serving users must not cut off everyone you have not got to yet. Enforcement is a separate,
deliberate switch, and it is an Admin's:

```sh
curl -s $A/admin/permissions/enforcement
```

```json
{"enforced": false}
```

Turning it on removes the catch-all: from then on, an identity with no grant has no access.
`GET $A/permissions/system` lists the rules Apchi owns, nobody may edit, and the reason each
one exists — worth reading before you turn enforcement on.

## Clean up

```sh
kubectl delete -f deploy/apchi-dev/
kubectl delete -f deploy/trino-dev/
```

The dev MongoDB is an `emptyDir`, so this discards every Snapshot with it.

## If something goes wrong

**Everything times out, or `apchi` never becomes ready.** Check that it is MongoDB and not
Apchi: `/api/v1/health` reports `503` while MongoDB is unreachable, and Apchi starts anyway
rather than crashlooping, so `kubectl logs deploy/apchi` tells you which it is. With nothing
reachable at all, startup takes about a minute before it starts answering — PyMongo spends
its own server-selection timeout first.

**A validation fails with "the validation coordinator was not serving within 300s".** Either
Apchi is running outside the cluster, where it cannot reach the probe's pod IP, or the node is
out of memory and the probe never started. `kubectl get pods -l apchi.dev/role=trino-validation`
while a validation is running shows whether the pod is there and what state it is in.

**An apply fails on a precondition.** Apchi refuses to touch a deployment it cannot configure
correctly and names what is wrong — a `subPath` mount on a file it has to update, a missing
catalog seed initContainer, a read-only volume over the catalog store, something else mounted
at a path Apchi owns, or a missing `security.refresh-period`. §16 of
`apchi_implementation.md` explains each, and `deploy/trino-dev/` satisfies all of them.

**Trino refuses to start after a rollout.** It should not get that far — validation starts a
real coordinator with the same files first. If it does, `kubectl logs deploy/trino-coordinator
-c trino` has the reason, near the **start** of the log rather than the end: Trino dumps its
whole configuration after the error.

## Where to go next

- [`concepts.md`](concepts.md) — the loop, the vocabulary, and who owns what.
- `localhost:8000/docs` — every endpoint, with the schemas.
- `apchi_implementation.md` — why Apchi is built the way it is. §7.1 is how configuration is
  delivered to a Trino deployment, §16 the preconditions Apchi checks before every apply.

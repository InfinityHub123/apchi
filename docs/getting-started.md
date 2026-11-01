# Getting started

This brings up a Trino cluster and an Apchi on your own machine and takes one catalog
through the whole configuration loop. Every command and every response below was run
against minikube with Trino 483; nothing here is illustrative.

Expect about ten minutes, most of it waiting for Trino to start.

## What you need

- `kubectl` and `helm`, and a cluster to point them at. minikube or kind is fine; this
  walkthrough uses minikube.
- Memory. Every JVM gets `-Xmx2G`, and during a validation there are
  three of them — coordinator, worker, and the throwaway coordinator the validation starts —
  so leave 6 GB free rather than 4. Short of that the probe pod takes longer than the 300s
  validation timeout to start, and the failure reads as "the validation coordinator was not
  serving" rather than as a memory problem.
- `curl` and `python3`, to read the JSON.

Everything lands in a namespace called `apchi`. That one namespace holds Trino and Apchi
both, because Apchi reads and writes exactly one namespace — the cluster's own — and nothing
outside it.

## The short version

If you just want it running:

```sh
./scripts/quickstart.sh
```

That builds the image into your cluster, deploys the reference Trino, installs the Helm
chart with a MongoDB alongside it, and prints what to do next. `--delete` removes all of it.
The rest of this page is the same thing done a step at a time, which is worth reading once
because each step explains what Apchi is doing.

## 1. Apchi

Apchi has to run **inside** the cluster, in the same namespace as Trino. A validation starts
a throwaway Trino coordinator and talks to it by pod IP, so an Apchi on your laptop reaches
Kubernetes fine and then times out every validation.

It also goes in **before** Trino, which is worth understanding rather than just following.
Apchi's chart creates the six Secrets Trino mounts its configuration from, because those
Secrets are Apchi's data — Apchi rewrites them on every apply. Trino reads two of them while
loading and refuses to boot when either is missing, so it cannot start and wait. Apchi has no
such problem: its readiness depends on MongoDB and nothing else, so it starts perfectly well
against a Trino that does not exist yet.

Build the image into your cluster's daemon:

```sh
kubectl create namespace apchi
minikube image build -t apchi:dev .
```

On kind, build with Docker and load the result instead:

```sh
docker build -t apchi:dev . && kind load docker-image apchi:dev
```

Then install the chart. `mongodb.deploy=true` runs a MongoDB alongside Apchi, which is fine
for trying it and loses every Snapshot when its pod restarts; point `mongodb.uri` at a real
one for anything else.

```sh
helm install apchi charts/apchi -n apchi \
  --set image.repository=apchi --set image.tag=dev \
  --set mongodb.deploy=true --wait
```

The two `image` settings are because you built the image locally; the chart otherwise
defaults to the published one, which your cluster cannot pull a locally built tag from.

Nothing here sets `auth.mode`, so this walkthrough runs with authentication **off** — every
caller is treated as an Admin, which is what makes the `curl` commands below work with no
credentials. That is the default and it is fine on a laptop; it is not fine on a cluster other
people can reach. `concepts.md` covers `password` and `sso`.

`charts/apchi/README.md` documents every value and when you would change it.

## 2. A Trino to configure

Apchi does not install Trino. It configures a Trino that is already deployed, and expects
that deployment to satisfy a handful of requirements: a catalog store it can write, the
Secrets Apchi writes mounted where Apchi will put them, and an access control file with a
refresh period on it. `charts/trino` is a Trino that satisfies all of them, small enough to
run on a laptop.

```sh
helm install trino charts/trino -n apchi --set fullnameOverride=trino --set worker.replicas=1 --wait
```

That chart exists to make the preconditions impossible to get wrong, and CI proves it by
rendering it and running Apchi's own precondition checker against the result.
`charts/trino/README.md` lists each guarantee and says what the chart deliberately leaves
out.

The two charts have to agree on **names** — the Trino Service, the two Deployments, and the
six Secrets. Nothing reconciles a mismatch: Apchi would write a Secret nobody reads and
report a successful apply the cluster never saw. The defaults line up, CI checks that they
do, and getting it wrong on a real cluster surfaces as a precondition failure rather than as
a lost change.

If you already run Trino on Kubernetes, read §7.1 and §16 of `apchi_implementation.md`
before pointing Apchi at it. Apchi refuses to apply against a deployment that does not
satisfy the preconditions and tells you which one failed, so the worst case is a clear
message rather than a lost change.

Reach the API through a port-forward:

```sh
kubectl -n apchi port-forward svc/apchi 8000:8000 &
curl -s localhost:8000/api/v1/health
```

```json
{"status":"ok","mongo":"up"}
```

This is Apchi's own liveness, not the cluster's — `mongo: "down"` comes with a 503, which is
the readiness signal Kubernetes acts on. Whether *Trino* is healthy is verification's job,
and you see that per apply.

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
kubectl exec -n apchi deploy/trino-coordinator -c trino -- trino --execute "SHOW CATALOGS"
```

```
"sales"
"system"
```

`charts/trino` seeds no catalogs by default, so `sales` and `system` is the whole list. If
you had seeded one with `bootstrap.catalogs`, or if you are pointing Apchi at a cluster that
already has catalogs, there is a consequence worth understanding first: the durable copy of
every catalog is a Secret Apchi owns and rewrites, and the coordinator reseeds its catalog
store from that Secret at every start. So a catalog Apchi does not hold survives the apply,
survives the day, and **disappears at the next coordinator restart**.

If that cluster's configuration is one you want to keep, **adopt it instead of applying over
it**: adoption reads what the coordinator is running, has you confirm anything it cannot
reconstruct, and produces Snapshot 1 from it. `POST /api/v1/admin/adoption/discoveries` starts
that, and §15 of `apchi_implementation.md` is the whole procedure.

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
./scripts/quickstart.sh --delete
```

or, if you installed it a step at a time:

```sh
helm uninstall trino -n apchi
helm uninstall apchi -n apchi
kubectl delete namespace apchi
```

Deleting the namespace is what removes the Secrets Apchi writes into. `helm uninstall`
deliberately leaves them: they hold the cluster's configuration and, for the catalog seed,
its only durable copy.

The MongoDB `mongodb.deploy` gives you is an `emptyDir`, so this discards every Snapshot
with it.

## Trusting a private CA

Apchi makes four outbound TLS connections — to the Trino coordinator, to MongoDB, to an OIDC
provider and to the alert webhook. One bundle covers all four:

```sh
kubectl create secret generic apchi-ca -n apchi --from-file=ca.crt=internal-ca.pem

helm upgrade apchi charts/apchi -n apchi --reuse-values \
  --set pod.caBundle.existingSecret=apchi-ca
```

**It is added to the public trust store, not substituted for it.** That matters more than it
sounds. A CA path handed to PyMongo, or to the Trino client's `requests` session, makes those
libraries trust that CA *alone* — so naming your internal CA would stop a public Slack webhook
verifying, and would leave Trino's liveness check and its queries disagreeing about what they
trust. Apchi concatenates the public store with your CA itself so that cannot happen, and your
file does not need to be a complete bundle.

**One bundle and not one per destination**, because an organisation with an internal PKI has
one root for all four. If you need several — an internal CA and a partner's — concatenate them
into the one file.

This is separate from `trino.tls` and `mongodb.tls` below, which are the certificates Apchi
*presents* rather than the ones it trusts.

## Reaching a MongoDB that requires TLS

Apchi's own storage. Give it the CA, and a certificate if MongoDB authenticates by one:

```sh
kubectl create secret generic apchi-mongo-tls -n apchi \
  --from-file=client.pem=apchi-client.pem

helm upgrade apchi charts/apchi -n apchi --reuse-values \
  --set mongodb.uri='mongodb://mongo-0.mongo:27017/?authMechanism=MONGODB-X509&replicaSet=rs0' \
  --set mongodb.tls.existingSecret=apchi-mongo-tls \
  --set mongodb.tls.certificateKeyKey=client.pem
```

**`client.pem` holds the certificate and its key in one file.** That is PyMongo's
`tlsCertificateKeyFile` shape, and it differs from `trino.tls` above, which wants the two
apart. `cat cert.pem key.pem > client.pem` is the whole of it.

**Either file turns TLS on.** There is no `tls=true` to remember — PyMongo builds its SSL
context from these.

**x509 is worth preferring where MongoDB offers it.** `mongodb.uri` is a plain value, so a
SCRAM password in it is visible in `helm get values` and in the pod spec. An x509 URI carries
no secret: MongoDB derives the username from the certificate's subject. Create the user in the
`$external` database named by that subject, in RFC2253 form — `CN=apchi` for a certificate
issued to that common name — and grant it `readWrite` and `dbAdmin` on the Apchi database.

## Reaching a coordinator over HTTPS

Apchi talks to Trino over plain HTTP by default, which is right for a coordinator on the pod
network with no authenticator. It is not an option on a cluster that authenticates its
callers: **every authentication type Trino supports requires TLS**, and with an authenticator
enabled Trino refuses plain HTTP from every client unless
`http-server.authentication.allow-insecure-over-http` is true — which it calls not
recommended. So on such a cluster Apchi does not get a worse connection, it gets none, and
§16 refuses the Apply rather than letting every query time out.

Give Apchi the URL and the PEM files:

```sh
kubectl create secret generic apchi-trino-tls -n apchi \
  --from-file=tls.crt=apchi.pem --from-file=tls.key=apchi-key.pem

helm upgrade apchi charts/apchi -n apchi --reuse-values \
  --set trino.url=https://trino:8443 \
  --set trino.tls.existingSecret=apchi-trino-tls \
  --set trino.tls.certificateKey=tls.crt --set trino.tls.keyKey=tls.key
```

Three things are worth knowing before you do:

**It is a URL, not a port.** The Trino client reads `https` from a port only when the port is
*exactly* 443, so setting a port of 8443 — the one Trino's own documentation uses — produced
plaintext requests against a TLS listener and reported nothing. `trino.url` carries the
scheme so that cannot happen.

**A private CA goes in `pod.caBundle`, not here.** One bundle covers every outbound
connection Apchi makes — see *Trusting a private CA* below. `trino.tls` is only the
certificate Apchi presents.

**The certificate is only needed for `http-server.authentication.type=CERTIFICATE`.** Trino
then demands one from every client, Apchi included, and the subject must be one the Cluster's
truststore accepts. It is not a Client Certificate in Apchi's sense — that term means a
certificate *Trino* presents when it connects outward to a data source, and points the other
way. Apchi mounts this one `0640` with an `fsGroup` matching the user it runs as, which is the
only combination that is both restricted and readable — a Secret volume's files are root-owned
whatever their mode, so `0600` would lock Apchi out of its own key.

## If something goes wrong

**Everything times out, or `apchi` never becomes ready.** Check that it is MongoDB and not
Apchi: `/api/v1/health` reports `503` while MongoDB is unreachable, and Apchi starts anyway
rather than crashlooping, so `kubectl logs -n apchi deploy/apchi` tells you which it is. With nothing
reachable at all, startup takes about a minute before it starts answering — PyMongo spends
its own server-selection timeout first.

**Apchi crashloops instead of reporting `503`.** The TLS settings above are read at startup
and not per request, so a `trino.tls` or `mongodb.tls` key naming a file the Secret does not
hold stops the process rather than degrading. `kubectl logs` names the path and which setting
put it there. This is the one startup failure that is not a 503, because there is nothing left
to serve it from — a 503 means MongoDB is unreachable, which is a different thing.

**Every Apply is refused with "Trino then refuses plain HTTP from every client".** The
coordinator authenticates its callers and Apchi is configured for `http://`. See
*Reaching a coordinator over HTTPS* below.

**A validation fails with "the validation coordinator was not serving within 300s".** Either
Apchi is running outside the cluster, where it cannot reach the probe's pod IP, or the node is
out of memory and the probe never started. `kubectl get pods -n apchi -l apchi.dev/role=trino-validation`
while a validation is running shows whether the pod is there and what state it is in.

**An apply fails on a precondition.** Apchi refuses to touch a deployment it cannot configure
correctly and names what is wrong — a `subPath` mount on a file it has to update, a missing
catalog seed initContainer, a read-only volume over the catalog store, something else mounted
at a path Apchi owns, or a missing `security.refresh-period`. §16 of
`apchi_implementation.md` explains each, and `charts/trino` satisfies all of them.

**Trino refuses to start after a rollout.** It should not get that far — validation starts a
real coordinator with the same files first. If it does, `kubectl logs -n apchi
deploy/trino-coordinator -c trino` has the reason, near the **start** of the log rather than the end: Trino dumps its
whole configuration after the error.

## Where to go next

- [`concepts.md`](concepts.md) — the loop, the vocabulary, and who owns what.
- `localhost:8000/docs` — every endpoint, with the schemas.
- `apchi_implementation.md` — why Apchi is built the way it is. §7.1 is how configuration is
  delivered to a Trino deployment, §16 the preconditions Apchi checks before every apply.

# Apchi for development

Apchi, a MongoDB for its Snapshots, and the RBAC that lets Apchi configure the Trino in
`deploy/trino-dev/`. Enough to follow `docs/getting-started.md`; not a production
deployment.

```sh
minikube image build -t apchi:dev .
kubectl apply -f deploy/apchi-dev/
kubectl rollout status deploy/apchi --timeout=300s
kubectl port-forward svc/apchi 8000:8000
```

## Why Apchi runs in the cluster

Not for convenience. A Validation creates an ephemeral Trino coordinator and asks it whether
it will accept the configuration, and it reaches that pod **by pod IP** — the pod has no
Service, because it exists for a few seconds and must not receive traffic. Pod IPs are not
routable from outside the cluster, so an Apchi on your laptop authenticates to Kubernetes
fine, creates the pod, and then reports that the validation coordinator was not serving
within 300s while the pod sits there serving perfectly.

Everything else Apchi does would work from outside. This does not, and validation is not
optional.

## What the Role grants, and why

`00-rbac.yaml` is the list of Kubernetes verbs Apchi actually uses, and it is worth reading
as documentation: it is the smallest set that lets Apchi do its job.

- **secrets** `get list create patch delete` — the generated configuration lives in Secrets
  Apchi patches. `create` and `delete` are the ephemeral Secret a Validation mounts on its
  probe; `list` is how an Apchi that crashed mid-validation finds the Secrets it leaked.
- **configmaps** `get` — read-only, and one check only: whether the access control
  properties the Admin mounted carry a refresh period. Apchi never writes a ConfigMap.
- **pods** `get list create delete` and **pods/log** `get` — the ephemeral validation
  coordinator. The log is the only place a refusing Trino explains itself.
- **deployments** `get patch` — a Rollout patches the coordinator Deployment and watches its
  status. Apchi never creates or deletes a Deployment: it does not own the Trino deployment.

No cluster-scoped rule, because Apchi reads and writes one namespace — the Cluster's own.

## Not production

- **One replica, and `Recreate`.** An Apply is in-process state — the task and the Candidate
  lock — so a second Apchi would run a second pipeline against one Trino. The old pod has to
  be gone before the new one starts.
- **MongoDB on an `emptyDir`.** Restarting that pod discards every Snapshot. Apchi's own
  durability requirement is §18 of `apchi_implementation.md`.
- **No authentication, on either Apchi or MongoDB.** Every Apchi endpoint is open, the Admin
  ones included.
- **`imagePullPolicy: IfNotPresent` and a `:dev` tag**, which is what makes
  `minikube image build` work and what makes this unsuitable anywhere real.

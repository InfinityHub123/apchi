# OpenShift and other restricted clusters

What Apchi needs on a cluster that runs OpenShift's `restricted-v2` SCC, or anything as
strict, and how to check a change against it without one.

## What such a cluster does

- **It assigns every pod an arbitrary uid** and ignores the image's `USER`.
- **It refuses a pod that pins `fsGroup`**, so no file can be handed to that uid through a
  group.
- **Some fleets add a resource policy**: requests and limits on every container, memory's
  equal.

The second has a consequence worth knowing before anything else. The kubelet ORs `0440` into
the mode of every file on a read-only Secret or ConfigMap volume, and the arbitrary uid is in
no group the file belongs to. So **a mounted credential is readable only if it is
world-readable**. A key that must stay private has to be copied by the process that reads it,
which is what Apchi does with the keys it presents to Trino (`app.trust`). The same holds for
anything else you mount.

## Values

```yaml
# Apchi's pod renders no fsGroup and mounts its credentials 0644; the Validation pod it
# creates is built without one; and Apchi accepts 0644 credential mounts on the Trino pods
# instead of refusing them.
restrictedPodSecurity: true

resources:
  requests: {cpu: 100m, memory: 512Mi}
  limits: {cpu: "1", memory: 512Mi}

validation:
  # The Validation pod is Apchi's to create, so this is the only place to give it limits.
  # Trino needs about 2Gi to start.
  resources:
    requests: {cpu: 500m, memory: 2Gi}
    limits: {cpu: "2", memory: 2Gi}

pod:
  caBundle:
    # A ConfigMap labelled config.openshift.io/inject-trusted-cabundle: "true", which the
    # platform fills with the cluster's trusted CAs. Create it once, empty, in Apchi's
    # namespace.
    configMap: trusted-ca
    key: ca-bundle.crt

mongodb:
  uri: mongodb://mongo.example:27017/?authMechanism=MONGODB-X509
  tls:
    existingSecret: apchi-mongo-client
    # The certificate and its key as the issuer wrote them. Apchi joins the two itself.
    certificateKey: tls.crt
    keyKey: tls.key
```

Nothing else changes. The image runs as any uid, and every pod the chart renders already sets
`runAsNonRoot`, the `RuntimeDefault` seccomp profile, no privilege escalation and drops every
capability. The bundled MongoDB (`mongodb.deploy`) is for trying Apchi out and is not built
for a restricted cluster: point `mongodb.uri` at your own.

## The Trino cluster

Trino has to run under the same rules, and Apchi's preconditions check the Trino pods before
every Apply. Two things to know:

- **`trinodb/trino` cannot start as an arbitrary uid unless `/data/trino` is writable**,
  because the image creates it for uid 1000. Mount an `emptyDir` there. Apchi does this for
  its own Validation pod.
- **Credential volumes on the Trino pods must be `0644`, or leave `defaultMode` unset.**
  Under `restrictedPodSecurity` Apchi accepts that and still refuses a writable mode. The
  cost is that a libpq-based client would refuse a world-readable key. Trino's JDBC connectors
  do not check.

## Checking a change without OpenShift

`policy.yaml` here emulates the parts of `restricted-v2` that Apchi's pods meet, using two
admission policies built into Kubernetes 1.36 and later:

```sh
kubectl apply -f deploy/restricted-v2/policy.yaml
```

From then on, Apchi's API pod and its Validation pods are assigned uid and gid `1000680000`.
A pod that pins `fsGroup`, sets a container `runAsUser`, lacks the security context above or
has no limits is refused. CI's tier 2 and its chart-install job both run under it.

It does not emulate SELinux or CRI-O, so a real OpenShift remains the final word.

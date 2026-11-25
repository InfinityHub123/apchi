# OpenShift and other restricted clusters

What Apchi needs on a cluster that runs OpenShift's `restricted-v2` SCC.

## What such a cluster does

- **It runs every pod as an arbitrary uid** and ignores the image's `USER`.
- **It refuses an `fsGroup` outside the namespace's range.** Trino's own group, 1000, is far
  below any range OpenShift allocates.
- **It adds the rest of the security context itself:** `runAsNonRoot`, the seccomp profile,
  the dropped capabilities. Nothing in Apchi's chart sets them.
- **Some fleets add a resource policy:** requests and limits on every container. Every
  container the chart renders takes them as a value, and renders none when it is unset.

A consequence worth knowing first: the kubelet ORs `0440` into the mode of every file on a
read-only Secret volume, and without an `fsGroup` the arbitrary uid is in no group the file
belongs to. So **a mounted credential is then readable only if it is world-readable**.

## Apchi

```yaml
# Apchi's pod renders no fsGroup and mounts its credentials 0644; the Validation pod it
# creates gets no fsGroup and a writable /data/trino; and Apchi accepts 0644 credential
# mounts on the Trino pods instead of refusing them.
restrictedPodSecurity: true

resources:
  requests: {cpu: 100m, memory: 512Mi}
  limits: {cpu: "1", memory: 512Mi}

validation:
  # The Validation pod is Apchi's to create, so this is the only place to give it limits.
  # Trino sizes its heap at 80% of the memory limit.
  resources:
    requests: {cpu: 500m, memory: 2Gi}
    limits: {cpu: "2", memory: 2Gi}

# Every pod: at /etc/pki/tls/certs/ca-bundle.crt, and Apchi adds it to what it trusts. A
# ConfigMap labelled config.openshift.io/inject-trusted-cabundle: "true", which the platform
# fills with the cluster's trusted CAs.
caBundle:
  configMap: trusted-ca
  key: ca-bundle.crt

# Every pod that runs Trino: at /etc/pki/java/truststore/cacerts, and the JVM is pointed at
# it. It replaces the JDK's own store, so it must hold the public roots too if a catalog
# reaches anything they sign.
javaTruststore:
  existingSecret: trino-truststore
  key: cacerts

mongodb:
  uri: mongodb://mongo.example:27017/?authMechanism=MONGODB-X509
  tls:
    existingSecret: apchi-mongo-client
    # The certificate and its key as the issuer wrote them. Apchi joins the two itself.
    certificateKey: tls.crt
    keyKey: tls.key
```

The image runs as any uid. The bundled MongoDB (`mongodb.deploy`) is for trying Apchi out and
is not built for a restricted cluster: point `mongodb.uri` at your own.

## Trino

The chart sets `node.data-dir=/data/trino` and mounts a writable volume there on both roles,
because the launcher writes its pid file and an `etc` symlink at that path and the image's own
copy belongs to uid 1000. What is left is the group and the limits:

```yaml
trino:
  # A group the namespace allows: the first of its
  # openshift.io/sa.scc.supplemental-groups annotation. With one, the credential volumes
  # stay 0640.
  fsGroup: 1000130000
  coordinator:
    resources: {requests: {cpu: "1", memory: 4Gi}, limits: {cpu: "2", memory: 4Gi}}
    # The initContainer that seeds the catalog store.
    seedResources: {requests: {cpu: 50m, memory: 64Mi}, limits: {cpu: 100m, memory: 64Mi}}
  worker:
    resources: {requests: {cpu: "1", memory: 4Gi}, limits: {cpu: "2", memory: 4Gi}}
```

`trino.fsGroup: null` renders none and lets the platform choose, and then the credential
volumes are unreadable at 0640 -- so Apchi accepts that only with `restrictedPodSecurity`, and
the cost is that every process in the pod can read the keys. A libpq-based client would refuse
such a key; Trino's JDBC connectors do not check.

## Routes

Set the hosts and the chart creates a Route for each, because the API serves
`route.openshift.io/v1`. Argo CD passes the cluster's API versions to the chart, so this holds
there too; only a manifest rendered with no cluster needs `expose.kind: route`.

```yaml
expose:
  apchi: {host: apchi.apps.example.com}
  trino: {host: trino.apps.example.com}
```

Both are `edge` by default, on the router's certificate. For a Trino that presents its own,
pass the connection through to it:

```yaml
expose:
  trino: {host: trino.apps.example.com, termination: passthrough}
trino:
  https: {enabled: true, existingSecret: trino-server-tls}
```

The initContainer that joins the certificate and the key takes `trino.coordinator.seedResources`,
the same limits as the one that seeds the catalog store.

## NetworkPolicy

`networkPolicy.enabled: true` restricts who reaches Trino's pods: Trino itself, Apchi's API
pods, and, when `expose.trino` is a Route, OpenShift's router. The router's namespace is
selected by both labels in use, `policy-group.network.openshift.io/ingress: ""` (OVN-Kubernetes)
and `network.openshift.io/policy-group: ingress` (OpenShift SDN), so nothing has to be set for
it. Anything else that queries Trino directly, such as a Trino Gateway in its own namespace,
goes in `networkPolicy.coordinator.from`:

```yaml
networkPolicy:
  enabled: true
  coordinator:
    from:
      - namespaceSelector:
          matchLabels: {kubernetes.io/metadata.name: trino-gateway}
```

It is off by default because turning it on cuts off every client not listed.

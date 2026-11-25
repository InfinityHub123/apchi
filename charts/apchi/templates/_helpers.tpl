{{- define "apchi.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "apchi.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "apchi.labels" -}}
app.kubernetes.io/name: {{ include "apchi.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{/*
Identifies the release. Never used alone as a selector: the release has more than one
workload in it when mongodb.deploy is on, and a Service selecting on these alone sent
half the API traffic to MongoDB. Always pair it with a component.
*/}}
{{- define "apchi.selectorLabels" -}}
app.kubernetes.io/name: {{ include "apchi.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "apchi.apiSelectorLabels" -}}
{{ include "apchi.selectorLabels" . }}
app.kubernetes.io/component: api
{{- end -}}

{{- define "apchi.mongoSelectorLabels" -}}
{{ include "apchi.selectorLabels" . }}
app.kubernetes.io/component: mongodb
{{- end -}}

{{- define "apchi.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "apchi.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "apchi.mongoName" -}}
{{- printf "%s-mongodb" (include "apchi.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
The MongoDB Apchi talks to. An explicit uri always wins, so a chart that also deploys
the dev MongoDB can still be pointed somewhere else; with neither, the install fails
rather than starting an Apchi that cannot keep a Snapshot.
*/}}
{{- define "apchi.mongoUri" -}}
{{- if .Values.mongodb.uri -}}
{{- .Values.mongodb.uri -}}
{{- else if .Values.mongodb.deploy -}}
{{- if .Values.mongodb.tls.existingSecret -}}
{{- /* The bundled MongoDB serves plaintext, so TLS files against it would make Apchi
       attempt a handshake nothing answers -- a crash at startup, with the cause two
       values apart. Said here instead. */ -}}
{{- fail "mongodb.tls is set but mongodb.deploy=true runs a plaintext MongoDB with no certificate. Point mongodb.uri at the TLS MongoDB instead, or clear mongodb.tls" -}}
{{- end -}}
{{- printf "mongodb://%s:27017" (include "apchi.mongoName" .) -}}
{{- else -}}
{{- fail "set mongodb.uri to your MongoDB, or mongodb.deploy=true to run one alongside Apchi for trying it out" -}}
{{- end -}}
{{- end -}}

{{/*
The mode of Apchi's own credential mounts. 420 is 0644: Helm prints integers in decimal.
*/}}
{{- define "apchi.credentialFileMode" -}}
{{- if .Values.restrictedPodSecurity }}420{{ else }}{{ .Values.pod.credentialFileMode }}{{ end }}
{{- end }}

{{/*
The Trino cluster. Its names are fixed by trino.name rather than derived from the release,
because Apchi reads its coordinator and worker Deployments by name and the Validation pod reads
the coordinator's image -- one value for all of them, and Apchi is configured from the same
helpers below, so the two cannot disagree.
*/}}
{{- define "trino.fullname" -}}
{{- .Values.trino.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "trino.labels" -}}
app.kubernetes.io/name: trino
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.trino.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "trino.selectorLabels" -}}
app.kubernetes.io/name: trino
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "trino.image" -}}
{{ .Values.trino.image.repository }}:{{ .Values.trino.image.tag }}
{{- end -}}

{{- define "trino.coordinatorName" -}}
{{- printf "%s-coordinator" (include "trino.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "trino.workerName" -}}
{{- printf "%s-worker" (include "trino.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
The discovery URI every node registers with. It is the coordinator Service, so a worker
that restarts finds the coordinator again without knowing a pod IP.
*/}}
{{- define "trino.discoveryUri" -}}
{{- printf "http://%s:%d" (include "trino.fullname" .) (int .Values.trino.service.port) -}}
{{- end -}}

{{/*
Where Apchi reaches the coordinator: trino.url when set -- a coordinator serving HTTPS, or
one behind something else -- and the Service otherwise.
*/}}
{{- define "trino.url" -}}
{{- .Values.trino.url | default (include "trino.discoveryUri" .) -}}
{{- end -}}

{{/*
Which authenticator reads the user-mapping file. Both variants feed the same file to the
same parser; Trino rejects the certificate one unless certificate authentication is
configured, which is the only reason there are two. §7.6.
*/}}
{{- define "trino.userMappingProperty" -}}
{{- if eq .Values.trino.authentication.type "certificate" -}}
http-server.authentication.certificate.user-mapping.file
{{- else if eq .Values.trino.authentication.type "insecure" -}}
http-server.authentication.insecure.user-mapping.file
{{- else -}}
{{- fail (printf "trino.authentication.type must be 'insecure' or 'certificate', not %q" .Values.trino.authentication.type) -}}
{{- end -}}
{{- end -}}

{{/*
What every pod trusts. Both optional: unset renders nothing at all. Each pod that gets them
mounts them at the same two paths, which are also Apchi's own constants (app.config), so the
Validation pod Apchi creates matches.
*/}}
{{- define "apchi.hasCaBundle" -}}
{{- $s := .Values.caBundle -}}
{{- if and $s.existingSecret $s.configMap -}}
{{- fail "caBundle: set existingSecret or configMap, not both" -}}
{{- end -}}
{{- if or $s.existingSecret $s.configMap -}}true{{- end -}}
{{- end -}}

{{- define "apchi.hasTruststore" -}}
{{- $s := .Values.javaTruststore -}}
{{- if and $s.existingSecret $s.configMap -}}
{{- fail "javaTruststore: set existingSecret or configMap, not both" -}}
{{- end -}}
{{- if and $s.password.existingSecret (not $s.password.key) -}}
{{- fail "javaTruststore.password: key is required with existingSecret" -}}
{{- end -}}
{{- if or $s.existingSecret $s.configMap -}}true{{- end -}}
{{- end -}}

{{/* One key of a ConfigMap or a Secret, under a fixed filename. */}}
{{- define "apchi.trustVolumeSource" -}}
{{- if .source.existingSecret -}}
secret:
  secretName: {{ .source.existingSecret | quote }}
  items:
    - {key: {{ .source.key | quote }}, path: {{ .path }}}
{{- else -}}
configMap:
  name: {{ .source.configMap | quote }}
  items:
    - {key: {{ .source.key | quote }}, path: {{ .path }}}
{{- end -}}
{{- end -}}

{{/* Called with (dict "root" $ "java" <bool>): the truststore only for pods that run Java. */}}
{{- define "apchi.trustVolumes" -}}
{{- $root := .root -}}
{{- if include "apchi.hasCaBundle" $root }}
- name: ca-bundle
  {{- include "apchi.trustVolumeSource" (dict "source" $root.Values.caBundle "path" "ca-bundle.crt") | nindent 2 }}
{{- end }}
{{- if and .java (include "apchi.hasTruststore" $root) }}
- name: java-truststore
  {{- include "apchi.trustVolumeSource" (dict "source" $root.Values.javaTruststore "path" "cacerts") | nindent 2 }}
{{- end }}
{{- end -}}

{{/*
The bundle as one file, at the path RHEL-based images read their system trust from -- which
replaces the image's own bundle there rather than adding to it. In trinodb/trino that path is a
symlink to /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem, and the kubelet mounts onto the
target: both paths then hold this bundle, though `ls` still shows the link. Measured. The truststore as a
directory, so a changed Secret or ConfigMap is projected into the pod without recreating it;
Java still reads it only at startup.
*/}}
{{- define "apchi.trustMounts" -}}
{{- $root := .root -}}
{{- if include "apchi.hasCaBundle" $root }}
- name: ca-bundle
  mountPath: /etc/pki/tls/certs/ca-bundle.crt
  subPath: ca-bundle.crt
  readOnly: true
{{- end }}
{{- if and .java (include "apchi.hasTruststore" $root) }}
- name: java-truststore
  mountPath: /etc/pki/java/truststore
  readOnly: true
{{- end }}
{{- end -}}

{{/*
The truststore's password, for a store that needs one to load. From its Secret and into the
JVM by Kubernetes' own $(VAR) expansion, so it is never in a ConfigMap or the pod spec.
*/}}
{{- define "apchi.truststorePasswordEnv" -}}
{{- $p := .Values.javaTruststore.password -}}
{{- if and (include "apchi.hasTruststore" .) $p.existingSecret }}
- name: TRUSTSTORE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ $p.existingSecret | quote }}
      key: {{ $p.key | quote }}
- name: JDK_JAVA_OPTIONS
  value: "-Djavax.net.ssl.trustStorePassword=$(TRUSTSTORE_PASSWORD)"
{{- end }}
{{- end -}}

{{/* The same two, as the JSON settings Apchi builds its Validation pod from. */}}
{{- define "apchi.validationCaBundle" -}}
{{- $s := .Values.caBundle -}}
{{- if $s.existingSecret -}}
{{- dict "secret" $s.existingSecret "key" $s.key | toJson -}}
{{- else -}}
{{- dict "config_map" $s.configMap "key" $s.key | toJson -}}
{{- end -}}
{{- end -}}

{{- define "apchi.validationTruststore" -}}
{{- $s := .Values.javaTruststore -}}
{{- $d := ternary (dict "secret" $s.existingSecret) (dict "config_map" $s.configMap) (ne $s.existingSecret "") -}}
{{- $_ := set $d "key" $s.key -}}
{{- if $s.password.existingSecret -}}
{{- $_ := set $d "password_secret" $s.password.existingSecret -}}
{{- $_ := set $d "password_key" $s.password.key -}}
{{- end -}}
{{- $d | toJson -}}
{{- end -}}

{{/*
Route or Ingress. `auto` asks the API being installed into, which only `helm install` and
`helm upgrade` can do: `helm template` and GitOps renderers see no cluster, find no Route API,
and would always choose an Ingress -- so they set `route` or `ingress` outright.
*/}}
{{- define "trino.httpsKeystore" -}}/data/trino-https/keystore.pem{{- end -}}

{{- define "apchi.exposeKind" -}}
{{- $kind := .Values.expose.kind -}}
{{- if eq $kind "auto" -}}
{{- if .Capabilities.APIVersions.Has "route.openshift.io/v1/Route" -}}route{{- else -}}ingress{{- end -}}
{{- else if has $kind (list "route" "ingress") -}}
{{- $kind -}}
{{- else -}}
{{- fail (printf "expose.kind must be auto, route or ingress, not %q" $kind) -}}
{{- end -}}
{{- end -}}

{{/*
Whether Trino is exposed through something that ends TLS in front of it. Trino then sees
plain HTTP from the router and, without trusting X-Forwarded-Proto, refuses password and
OAuth authentication as insecure.
*/}}
{{- define "trino.behindTlsProxy" -}}
{{- $e := .Values.expose.trino -}}
{{- if and $e.host (ne ($e.termination | default "edge") "passthrough") -}}true{{- end -}}
{{- end -}}

{{/*
The CA a `reencrypt` Route verifies its backend with: the value given, or the chart's
caBundle as it exists in the cluster. `lookup` sees the cluster only on install and upgrade,
so a rendered manifest carries no CA and the router falls back to the service CA.
*/}}
{{- define "apchi.reencryptCa" -}}
{{- $root := .root -}}
{{- if .exposure.destinationCACertificate -}}
{{- .exposure.destinationCACertificate -}}
{{- else if include "apchi.hasCaBundle" $root -}}
{{- $s := $root.Values.caBundle -}}
{{- if $s.configMap -}}
{{- $found := lookup "v1" "ConfigMap" $root.Release.Namespace $s.configMap -}}
{{- if $found -}}{{- index ($found.data | default dict) $s.key | default "" -}}{{- end -}}
{{- else -}}
{{- $found := lookup "v1" "Secret" $root.Release.Namespace $s.existingSecret -}}
{{- if $found -}}{{- index ($found.data | default dict) $s.key | default "" | b64dec -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
The annotations an Ingress controller needs for the terminations Ingress has no field for.
Only the controllers named; any other takes its own through `annotations`.
*/}}
{{- define "apchi.ingressTerminationAnnotations" -}}
{{- $controller := .controller -}}
{{- if eq .termination "passthrough" -}}
{{- if eq $controller "nginx" }}
nginx.ingress.kubernetes.io/ssl-passthrough: "true"
{{- else if eq $controller "haproxy" }}
haproxy.org/ssl-passthrough: "true"
{{- end }}
{{- else if eq .termination "reencrypt" -}}
{{- if eq $controller "nginx" }}
nginx.ingress.kubernetes.io/backend-protocol: "HTTPS"
{{- else if eq $controller "haproxy" }}
haproxy.org/server-ssl: "true"
{{- end }}
{{- end -}}
{{- end -}}

{{/*
One Route or one Ingress in front of one Service, or nothing when no host is set. `edge`
reaches the Service's `http` port; `passthrough` and `reencrypt` reach `https`, because the
router then speaks TLS to the pod.
*/}}
{{- define "apchi.exposure" -}}
{{- $root := .root -}}
{{- $e := .exposure -}}
{{- if $e.host }}
{{- $termination := $e.termination | default "edge" -}}
{{- if not (has $termination (list "edge" "passthrough" "reencrypt")) -}}
{{- fail (printf "expose.%s.termination must be edge, passthrough or reencrypt, not %q" .component $termination) -}}
{{- end -}}
{{- if and (ne $termination "edge") (not .https) -}}
{{- if eq .component "trino" -}}
{{- fail (printf "expose.trino.termination %s sends TLS to the pod: set trino.https.enabled and trino.https.existingSecret, or use edge." $termination) -}}
{{- else -}}
{{- fail (printf "expose.%s.termination %s sends TLS to the pod, and %s serves only plain HTTP. Use edge." .component $termination .component) -}}
{{- end -}}
{{- end -}}
{{- $port := ternary "http" "https" (eq $termination "edge") -}}
{{- $controller := $root.Values.expose.ingress.controller -}}
{{- if not (has $controller (list "" "nginx" "haproxy")) -}}
{{- fail (printf "expose.ingress.controller must be nginx, haproxy or empty, not %q" $controller) -}}
{{- end }}
---
{{- if eq (include "apchi.exposeKind" $root) "route" }}
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: {{ .name }}
  labels: {{- .labels | nindent 4 }}
  {{- with $e.annotations }}
  annotations: {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  host: {{ $e.host | quote }}
  to:
    kind: Service
    name: {{ .service }}
    weight: 100
  port:
    targetPort: {{ $port }}
  tls:
    termination: {{ $termination }}
    {{- if ne $termination "passthrough" }}
    insecureEdgeTerminationPolicy: Redirect
    {{- end }}
    {{- if eq $termination "reencrypt" }}
    {{- with include "apchi.reencryptCa" (dict "root" $root "exposure" $e) }}
    destinationCACertificate: |
      {{- . | trim | nindent 6 }}
    {{- end }}
    {{- end }}
  wildcardPolicy: None
{{- else }}
{{- $annotations := include "apchi.ingressTerminationAnnotations" (dict "controller" $controller "termination" $termination) | fromYaml | default dict }}
{{- $annotations = merge (deepCopy ($e.annotations | default dict)) $annotations }}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ .name }}
  labels: {{- .labels | nindent 4 }}
  {{- with $annotations }}
  annotations: {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  {{- with $root.Values.expose.ingress.className }}
  ingressClassName: {{ . | quote }}
  {{- end }}
  {{- if ne $termination "passthrough" }}
  tls:
    - hosts: [{{ $e.host | quote }}]
      {{- with $e.tlsSecret }}
      secretName: {{ . | quote }}
      {{- end }}
  {{- end }}
  rules:
    - host: {{ $e.host | quote }}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ .service }}
                port:
                  name: {{ $port }}
{{- end }}
{{- end }}
{{- end -}}

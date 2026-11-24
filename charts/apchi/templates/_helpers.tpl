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

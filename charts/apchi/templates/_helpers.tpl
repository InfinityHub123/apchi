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
{{- printf "mongodb://%s:27017" (include "apchi.mongoName" .) -}}
{{- else -}}
{{- fail "set mongodb.uri to your MongoDB, or mongodb.deploy=true to run one alongside Apchi for trying it out" -}}
{{- end -}}
{{- end -}}

{{- define "trino.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
The names everything is derived from. fullnameOverride matters more here than in most
charts: Apchi is configured with the coordinator and worker Deployment names, so being able
to fix them independently of the release name is what lets one Apchi configuration work
whatever the release is called.
*/}}
{{- define "trino.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "trino.labels" -}}
app.kubernetes.io/name: {{ include "trino.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "trino.selectorLabels" -}}
app.kubernetes.io/name: {{ include "trino.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "trino.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{/* Apchi reads the coordinator and worker Deployments by name, so both are derived once. */}}
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
{{- printf "http://%s:%d" (include "trino.fullname" .) (int .Values.service.port) -}}
{{- end -}}

{{/*
Which authenticator reads the user-mapping file. Both variants feed the same file to the
same parser; Trino rejects the certificate one unless certificate authentication is
configured, which is the only reason there are two. §7.6.
*/}}
{{- define "trino.userMappingProperty" -}}
{{- if eq .Values.authentication.type "certificate" -}}
http-server.authentication.certificate.user-mapping.file
{{- else if eq .Values.authentication.type "insecure" -}}
http-server.authentication.insecure.user-mapping.file
{{- else -}}
{{- fail (printf "authentication.type must be 'insecure' or 'certificate', not %q" .Values.authentication.type) -}}
{{- end -}}
{{- end -}}

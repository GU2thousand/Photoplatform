{{- define "photo.name" -}}{{ .Release.Name }}{{- end -}}
{{- define "photo.labels" -}}
app.kubernetes.io/name: photoplatform
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/part-of: photoplatform
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ .root.Chart.Name }}-{{ .root.Chart.Version }}
{{- end -}}
{{- define "photo.selector" -}}
app.kubernetes.io/name: photoplatform
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}
{{- define "photo.image" -}}{{ .repository }}@{{ .digest }}{{- end -}}
{{- define "photo.annotations" -}}
photoplatform.io/commit: {{ .Values.release.commitSha | quote }}
photoplatform.io/revision: {{ .Values.release.commitSha | quote }}
{{- end -}}
{{- define "photo.security" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile: {type: RuntimeDefault}
{{- end -}}
{{- define "photo.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities: {drop: [ALL]}
{{- end -}}
{{- define "photo.secretVolume" -}}
- name: runtime-secrets
  {{- if eq .root.Values.secrets.provider "csi" }}
  csi:
    driver: secrets-store.csi.k8s.io
    readOnly: true
    volumeAttributes:
      secretProviderClass: {{ .root.Release.Name }}-{{ .component }}
  {{- else }}
  secret:
    secretName: {{ .secret.existingSecret }}
    defaultMode: 0440
    items:
      {{- range .secret.keys }}
      - key: {{ . }}
        path: {{ . }}
      {{- end }}
  {{- end }}
{{- end -}}
{{- define "photo.secretEnv" -}}
{{- range .keys }}
- name: {{ . }}_FILE
  value: /mnt/secrets/{{ . }}
{{- end }}
{{- end -}}
{{- define "photo.secretClass" -}}
apiVersion: secrets-store.csi.x-k8s.io/v1
kind: SecretProviderClass
metadata:
  name: {{ .root.Release.Name }}-{{ .component }}
  labels:
    {{- include "photo.labels" . | nindent 4 }}
spec:
  provider: aws
  parameters:
    region: {{ .root.Values.aws.region | quote }}
    usePodIdentity: "true"
    objects: |
      - objectName: {{ .secret.arn | quote }}
        objectType: secretsmanager
        objectVersion: {{ .secret.versionId | quote }}
        filePermission: "0444"
        jmesPath:
          {{- range .secret.keys }}
          - path: {{ . | quote }}
            objectAlias: {{ . | quote }}
            filePermission: "0444"
          {{- end }}
{{- end -}}
{{- define "photo.publicEnv" -}}
- name: SPRING_PROFILES_ACTIVE
  value: {{ ternary "aws,eks" "kubernetes-local" (eq .Values.runtimeMode "aws") | quote }}
- name: AWS_REGION
  value: {{ .Values.aws.region | quote }}
- name: AWS_DEFAULT_REGION
  value: {{ .Values.aws.region | quote }}
- name: SPRING_DATASOURCE_URL
  value: {{ printf "jdbc:postgresql://%s:%v/%s" .Values.config.databaseHost .Values.config.databasePort .Values.config.databaseName | quote }}
- name: DATABASE_HOST
  value: {{ .Values.config.databaseHost | quote }}
- name: DATABASE_PORT
  value: {{ .Values.config.databasePort | quote }}
- name: DATABASE_NAME
  value: {{ .Values.config.databaseName | quote }}
- name: DATABASE_SSL_MODE
  value: {{ .Values.config.databaseSslMode | quote }}
- name: RABBITMQ_HOST
  value: {{ .Values.config.rabbitmqHost | quote }}
- name: RABBITMQ_PORT
  value: {{ .Values.config.rabbitmqPort | quote }}
- name: RABBITMQ_SSL_ENABLED
  value: {{ .Values.config.rabbitmqTls | quote }}
- name: STORAGE_PROVIDER
  value: {{ ternary "aws" "minio" (eq .Values.runtimeMode "aws") | quote }}
- name: STORAGE_REGION
  value: {{ .Values.aws.region | quote }}
- name: STORAGE_BUCKET
  value: {{ .Values.config.storageBucket | quote }}
- name: STORAGE_PREFIX
  value: {{ .Values.config.storagePrefix | quote }}
- name: STORAGE_PATH_STYLE_ACCESS
  value: {{ ternary "false" "true" (eq .Values.runtimeMode "aws") | quote }}
- name: STORAGE_AUTO_CREATE_BUCKET
  value: {{ ternary "false" "true" (eq .Values.runtimeMode "aws") | quote }}
- name: APP_CORS_ALLOWED_ORIGINS
  value: {{ .Values.config.corsAllowedOrigins | quote }}
- name: APP_ENVIRONMENT
  value: {{ .Values.environment | quote }}
- name: APP_SEED_ENABLED
  value: "false"
- name: SPRING_FLYWAY_ENABLED
  value: "false"
- name: JPA_DDL_AUTO
  value: validate
- name: MEDIA_PIPELINE_ENABLED
  value: "true"
- name: SEMANTIC_SEARCH_ENABLED
  value: {{ .Values.ml.enabled | quote }}
- name: CLIP_MODEL_VERSION
  value: {{ .Values.config.modelVersion | quote }}
- name: ENCODER_URL
  value: http://{{ .Release.Name }}-encoder:8090
- name: CDN_DOMAIN
  value: {{ .Values.config.cdnDomain | quote }}
- name: CDN_KEY_PAIR_ID
  value: {{ .Values.config.cdnKeyPairId | quote }}
- name: MEDIA_URL_PROVIDER
  value: {{ .Values.config.mediaUrlProvider | quote }}
{{- if eq .Values.runtimeMode "local" }}
- name: STORAGE_ENDPOINT
  value: {{ .Values.config.storageEndpoint | quote }}
- name: STORAGE_PUBLIC_ENDPOINT
  value: {{ .Values.config.storagePublicEndpoint | quote }}
{{- end }}
{{- end -}}
{{- define "photo.scheduling" -}}
{{- $root := .root -}}
{{- $selector := .nodeSelector -}}
{{- if and $selector (eq $root.Values.runtimeMode "aws") }}
nodeSelector:
  {{- toYaml $selector | nindent 2 }}
{{- end }}
{{- $tolerations := .tolerations | default $root.Values.tolerations -}}
{{- if $tolerations }}
tolerations:
  {{- toYaml $tolerations | nindent 2 }}
{{- end }}
{{- if $root.Values.topology.enabled }}
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: {{ $root.Values.topology.whenUnsatisfiable }}
    labelSelector:
      matchLabels:
        {{- include "photo.selector" . | nindent 8 }}
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: {{ $root.Values.topology.whenUnsatisfiable }}
    labelSelector:
      matchLabels:
        {{- include "photo.selector" . | nindent 8 }}
{{- end }}
{{- end -}}
{{- define "photo.validate" -}}
{{- $v := .Values -}}
{{- if ne .Release.Namespace (printf "photoplatform-%s" $v.environment) }}{{ fail "namespace must match environment: photoplatform-dev or photoplatform-prod" }}{{ end -}}
{{- if not (regexMatch "^[a-f0-9]{40}$" $v.release.commitSha) }}{{ fail "release.commitSha must be a full verified source SHA" }}{{ end -}}
{{- $roles := dict "api" $v.secrets.api "media-worker" $v.secrets.mediaWorker "migrator" $v.secrets.migrator -}}
{{- if $v.ml.enabled }}{{- $_ := set $roles "embedding-worker" $v.secrets.embeddingWorker -}}{{- $_ := set $roles "encoder" $v.secrets.encoder -}}{{- end -}}
{{- if $v.queueCollector.enabled }}{{- $_ := set $roles "queue-collector" $v.secrets.queueCollector -}}{{- end -}}
{{- range $role, $secret := $roles }}
  {{- if eq $v.secrets.provider "csi" }}
    {{- $prefix := printf "arn:aws:secretsmanager:%s:%s:secret:" $v.aws.region $v.aws.accountId -}}
    {{- if not (hasPrefix $prefix $secret.arn) }}{{ fail (printf "%s Secret ARN must match expected account and region" $role) }}{{ end -}}
    {{- if not $secret.versionId }}{{ fail (printf "%s requires an immutable Secrets Manager versionId" $role) }}{{ end -}}
  {{- else }}
    {{- if not $secret.existingSecret }}{{ fail (printf "%s requires existingSecret" $role) }}{{ end -}}
  {{- end }}
{{- end -}}
{{- $images := dict "api" $v.images.api "mediaWorker" $v.images.mediaWorker -}}
{{- if $v.ml.enabled }}{{- $_ := set $images "ml" $v.images.ml -}}{{- end -}}
{{- if $v.queueCollector.enabled }}{{- $_ := set $images "queueCollector" $v.images.queueCollector -}}{{- end -}}
{{- range $name, $image := $images }}
  {{- if not (regexMatch "^sha256:[a-f0-9]{64}$" $image.digest) }}{{ fail (printf "%s image requires sha256 digest" $name) }}{{ end -}}
  {{- if not $image.repository }}{{ fail (printf "%s repository required" $name) }}{{ end -}}
  {{- if eq $v.runtimeMode "aws" }}
    {{- $ecr := printf "%s.dkr.ecr.%s.amazonaws.com/" $v.aws.accountId $v.aws.region -}}
    {{- if not (hasPrefix $ecr $image.repository) }}{{ fail (printf "%s image must use this account and region ECR" $name) }}{{ end -}}
  {{- end -}}
{{- end -}}
{{- if $v.telemetry.enabled }}
  {{- if not $v.telemetry.rbacProvisioned }}{{ fail "telemetry requires platform-owned namespace read-only RBAC" }}{{ end -}}
  {{- range $name := list "prometheus" "otel" "kubeStateMetrics" }}
    {{- if not (regexMatch "^sha256:[a-f0-9]{64}$" (index $v.images $name).digest) }}{{ fail (printf "%s requires immutable image digest" $name) }}{{ end -}}
  {{- end -}}
  {{- if not (hasPrefix "https://" $v.telemetry.otlpExporterEndpoint) }}{{ fail "telemetry.otlpExporterEndpoint must be TLS HTTPS" }}{{ end -}}
{{- end -}}
{{- if and $v.queueCollector.enabled (not $v.queueCollector.rbacProvisioned) }}{{ fail "queue collector requires platform-owned namespace Pod list RBAC" }}{{ end -}}
{{- if eq $v.runtimeMode "aws" }}
  {{- if ne .Release.Name "photoplatform" }}{{ fail "AWS release name must be photoplatform to match Terraform Pod Identity associations" }}{{ end -}}
  {{- if ne $v.secrets.provider "csi" }}{{ fail "AWS requires CSI mounted Secrets with Pod Identity" }}{{ end -}}
  {{- if not (regexMatch "^[0-9]{12}$" $v.aws.accountId) }}{{ fail "aws.accountId must be 12 digits" }}{{ end -}}
  {{- if not (regexMatch "^[a-z]{2}-[a-z]+-[0-9]+$" $v.aws.region) }}{{ fail "aws.region required" }}{{ end -}}
  {{- if not $v.aws.clusterName }}{{ fail "aws.clusterName required" }}{{ end -}}
  {{- if or (ne $v.config.databaseSslMode "verify-full") (not $v.config.rabbitmqTls) (ne (int $v.config.rabbitmqPort) 5671) }}{{ fail "AWS requires DB verify-full and RabbitMQ TLS port 5671" }}{{ end -}}
  {{- if or $v.config.storageEndpoint $v.config.storagePublicEndpoint }}{{ fail "AWS forbids storage endpoint overrides" }}{{ end -}}
  {{- if or (not $v.network.enabled) $v.network.allowLocalDependencies }}{{ fail "AWS requires network policies and external managed dependencies" }}{{ end -}}
  {{- if or (not $v.network.ingressCidrs) (not $v.network.databaseCidrs) (not $v.network.brokerCidrs) }}{{ fail "AWS requires explicit ALB, DB and MQ source/destination CIDRs" }}{{ end -}}
  {{- if and $v.queueCollector.enabled (or (ne $v.queueCollector.managementScheme "https") (not $v.network.kubernetesApiCidrs)) }}{{ fail "collector requires MQ HTTPS and explicit Kubernetes API CIDRs" }}{{ end -}}
  {{- range $role, $secret := $roles }}
    {{- range $secret.keys }}
      {{- if or (eq . "STORAGE_ACCESS_KEY") (eq . "STORAGE_SECRET_KEY") (hasPrefix "AWS_" .) }}{{ fail "AWS forbids static storage credentials" }}{{ end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- if eq $v.environment "prod" }}
  {{- if $v.api.exposeInstanceId }}{{ fail "production forbids exposing internal instance identity headers" }}{{ end -}}
  {{- if ne $v.runtimeMode "aws" }}{{ fail "production only supports AWS runtime" }}{{ end -}}
  {{- if or (lt (int $v.api.replicas) 2) (lt (int $v.api.autoscaling.minReplicas) 2) }}{{ fail "production API requires at least two replicas" }}{{ end -}}
  {{- if not $v.ingress.enabled }}{{ fail "production requires HTTPS ALB ingress" }}{{ end -}}
  {{- if or (not $v.topology.enabled) (ne $v.topology.whenUnsatisfiable "DoNotSchedule") }}{{ fail "production requires enforced topology spread" }}{{ end -}}
  {{- range concat $v.network.ingressCidrs $v.network.databaseCidrs $v.network.brokerCidrs }}
    {{- if eq . "0.0.0.0/0" }}{{ fail "production forbids unrestricted dependency/ALB CIDRs" }}{{ end -}}
  {{- end -}}
{{- end -}}
{{- if $v.ingress.enabled }}
  {{- if or (not $v.ingress.host) (not (hasPrefix (printf "arn:aws:acm:%s:%s:certificate/" $v.aws.region $v.aws.accountId) $v.ingress.certificateArn)) }}{{ fail "ingress requires hostname and same-account/region ACM certificate ARN" }}{{ end -}}
{{- end -}}
{{- if or (not $v.config.databaseHost) (not $v.config.rabbitmqHost) (not $v.config.storageBucket) (not $v.config.corsAllowedOrigins) }}{{ fail "database, broker, bucket and explicit CORS origin required" }}{{ end -}}
{{- if and $v.ml.enabled (not (has "ENCODER_TOKEN" $v.secrets.api.keys)) }}{{ fail "ML requires API ENCODER_TOKEN mounted secret" }}{{ end -}}
{{- if and $v.ml.enabled (not (has "ENCODER_TOKEN" $v.secrets.encoder.keys)) }}{{ fail "encoder requires ENCODER_TOKEN mounted secret" }}{{ end -}}
{{- if and $v.queueCollector.enabled (or (not (has "RABBITMQ_USER" $v.secrets.queueCollector.keys)) (not (has "RABBITMQ_PASSWORD" $v.secrets.queueCollector.keys))) }}{{ fail "collector requires readonly MQ user/password mounted Secret" }}{{ end -}}
{{- if and $v.ml.enabled (ne $v.config.modelVersion "clip-vit-b32-openai-v1") }}{{ fail "ML requires the audited fixed CLIP model version" }}{{ end -}}
{{- range $key := list "SPRING_DATASOURCE_USERNAME" "SPRING_DATASOURCE_PASSWORD" "APP_JWT_SECRET" "RABBITMQ_USER" "RABBITMQ_PASSWORD" }}
  {{- if not (has $key $v.secrets.api.keys) }}{{ fail (printf "API required mounted secret %s missing" $key) }}{{ end -}}
{{- end -}}
{{- range $role := list "media-worker" "embedding-worker" }}
  {{- if hasKey $roles $role }}
    {{- range $key := list "DATABASE_USER" "DATABASE_PASSWORD" "RABBITMQ_USER" "RABBITMQ_PASSWORD" }}
      {{- if not (has $key (index $roles $role).keys) }}{{ fail (printf "%s mounted secret %s missing" $role $key) }}{{ end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- if or (not (has "MIGRATOR_DATABASE_USERNAME" $v.secrets.migrator.keys)) (not (has "MIGRATOR_DATABASE_PASSWORD" $v.secrets.migrator.keys)) }}{{ fail "migrator requires dedicated credentials" }}{{ end -}}
{{- if and (eq $v.runtimeMode "aws") (eq $v.secrets.api.arn $v.secrets.migrator.arn) }}{{ fail "migrator must not share the API runtime Secret" }}{{ end -}}
{{- if or (gt (int $v.api.dbPoolMinIdle) (int $v.api.dbPoolMax)) (gt (int $v.api.autoscaling.minReplicas) (int $v.api.autoscaling.maxReplicas)) }}{{ fail "invalid DB pool or HPA range" }}{{ end -}}
{{- if and (eq $v.config.mediaUrlProvider "cloudfront") (or (not $v.config.cdnDomain) (not $v.config.cdnKeyPairId) (not (has "CDN_PRIVATE_KEY_PEM" $v.secrets.api.keys))) }}{{ fail "CloudFront requires domain, key pair ID and private key mounted Secret" }}{{ end -}}
{{- if gt (int $v.api.preStopSeconds | add (int $v.api.shutdownSeconds) (int $v.ingress.deregistrationDelaySeconds)) (int $v.api.terminationGracePeriodSeconds) }}{{ fail "API stop budget exceeds termination grace" }}{{ end -}}
{{- if lt (int $v.mediaWorker.terminationGracePeriodSeconds) (add (int $v.mediaWorker.drainSeconds) 20) }}{{ fail "worker termination grace must include drain plus safety margin" }}{{ end -}}
{{- if gt (int $v.mediaWorker.replicas) (int $v.mediaWorker.maxReplicas) }}{{ fail "media replicas exceed connection budget maximum" }}{{ end -}}
{{- if and $v.ml.enabled (gt (int $v.ml.embeddingReplicas) (int $v.ml.maxEmbeddingReplicas)) }}{{ fail "embedding replicas exceed budget maximum" }}{{ end -}}
{{- $apiMax := int $v.api.replicas -}}{{- if $v.api.autoscaling.enabled }}{{- $apiMax = int $v.api.autoscaling.maxReplicas -}}{{- end -}}
{{- $workerMax := add (int $v.mediaWorker.maxReplicas) 1 -}}{{- if $v.ml.enabled }}{{- $workerMax = add $workerMax (int $v.ml.maxEmbeddingReplicas) 1 -}}{{- end -}}
{{- $total := add (mul (add $apiMax 1) (int $v.api.dbPoolMax)) (mul $workerMax 4) (int $v.capacity.probeReserve) (int $v.capacity.migrationReserve) (int $v.capacity.administrativeReserve) -}}
{{- if gt $total (int $v.capacity.databaseConnectionBudget) }}{{ fail (printf "database connection budget exceeded: %d > %d" $total (int $v.capacity.databaseConnectionBudget)) }}{{ end -}}
{{- end -}}

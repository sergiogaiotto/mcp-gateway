{{/*
Funções do chart do RaPA. Os nomes dos recursos são fixos a partir de nameBase (rapa-web,
rapa-api, rapa-despachante, ...), porque outros componentes (os papéis do OpenBao, as
NetworkPolicies, o RBAC no namespace das execuções) se referem a eles.
*/}}

{{- define "rapa.name" -}}
{{- .Values.nameBase | trunc 40 | trimSuffix "-" -}}
{{- end -}}

{{- define "rapa.component" -}}
{{- printf "%s-%s" (include "rapa.name" .root) .component -}}
{{- end -}}

{{- define "rapa.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Rótulos de todos os recursos do chart. */}}
{{- define "rapa.labels" -}}
app.kubernetes.io/name: rapa
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: rapa
helm.sh/chart: {{ include "rapa.chart" . }}
{{- end -}}

{{/* Rótulos que escolhem os pods de um componente (root, component). */}}
{{- define "rapa.selectorLabels" -}}
app.kubernetes.io/name: rapa
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "rapa.componentLabels" -}}
{{ include "rapa.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* Referência de uma imagem: o digest, quando há, vale no lugar da tag. */}}
{{- define "rapa.imageRef" -}}
{{- $image := .image -}}
{{- if $image.digest -}}
{{- printf "%s@%s" $image.repository $image.digest -}}
{{- else -}}
{{- printf "%s:%s" $image.repository (default .root.Chart.AppVersion $image.tag) -}}
{{- end -}}
{{- end -}}

{{- define "rapa.controlImage" -}}
{{- include "rapa.imageRef" (dict "image" .Values.images.control "root" .) -}}
{{- end -}}

{{- define "rapa.executorImage" -}}
{{- include "rapa.imageRef" (dict "image" .Values.images.executor "root" .) -}}
{{- end -}}

{{/* Segurança restrita de todo pod do chart (não root, seccomp). */}}
{{- define "rapa.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
fsGroupChangePolicy: OnRootMismatch
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/* Segurança restrita de todo contêiner do chart (só leitura, sem capacidades). */}}
{{- define "rapa.containerSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
allowPrivilegeEscalation: false
privileged: false
readOnlyRootFilesystem: true
capabilities:
  drop:
    - ALL
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "rapa.imagePullSecrets" -}}
{{- with .Values.images.pullSecrets }}
imagePullSecrets:
{{- range . }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end -}}

{{/* Espalha as réplicas pelos nós (e pelas zonas, quando houver). */}}
{{- define "rapa.topologySpread" -}}
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels:
        {{- include "rapa.selectorLabels" . | nindent 8 }}
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels:
        {{- include "rapa.selectorLabels" . | nindent 8 }}
{{- end -}}

{{/* Uma variável a partir de um Secret existente (root, name, secret, key). */}}
{{- define "rapa.secretEnv" -}}
- name: {{ .name }}
  valueFrom:
    secretKeyRef:
      name: {{ .secret }}
      key: {{ .key }}
{{- end -}}

{{/*
A URL do banco de um papel (root, role, var). A senha vem do Secret do papel numa variável
própria e entra na URL pela expansão $(VAR) do Kubernetes: o valor nunca aparece no manifesto.
*/}}
{{- define "rapa.databaseEnv" -}}
{{- $db := .root.Values.database -}}
{{- $role := index $db.roles .role -}}
{{- $password := printf "BANCO_SENHA_%s" (upper .role) -}}
- name: {{ $password }}
  valueFrom:
    secretKeyRef:
      name: {{ required (printf "database.roles.%s.existingSecret é obrigatório" .role) $role.existingSecret }}
      key: {{ $db.passwordKey }}
{{- $query := printf "sslmode=%s" $db.sslMode -}}
{{- if $db.caSecret.name -}}
{{- $query = printf "%s&sslrootcert=/var/run/rapa/banco/%s" $query $db.caSecret.key -}}
{{- end }}
- name: {{ .var }}
  value: {{ printf "postgresql://%s:$(%s)@%s:%v/%s?%s" $role.name $password $db.host $db.port $db.name $query | quote }}
{{- end -}}

{{- define "rapa.databaseVolume" -}}
{{- if .Values.database.caSecret.name }}
- name: banco-ca
  secret:
    secretName: {{ .Values.database.caSecret.name }}
    defaultMode: 0444
    items:
      - key: {{ .Values.database.caSecret.key }}
        path: {{ .Values.database.caSecret.key }}
{{- end }}
{{- end -}}

{{- define "rapa.databaseMount" -}}
{{- if .Values.database.caSecret.name }}
- name: banco-ca
  mountPath: /var/run/rapa/banco
  readOnly: true
{{- end }}
{{- end -}}

{{/*
O endereço do canal desta réplica do despachante: {ip} é trocado pelo RaPA pelo IP do pod
(com colchetes, se for IPv6). Um endereço sem {ip} (um Service) é recusado na partida.
*/}}
{{- define "rapa.channelAddress" -}}
{{- printf "http://{ip}:%v" .Values.executions.channel.port -}}
{{- end -}}

{{/* O token projetado da conta de serviço para o OpenBao (RAPA_BAO_TOKEN_SA). */}}
{{- define "rapa.baoTokenPath" -}}
/var/run/secrets/rapa/bao/token
{{- end -}}

{{/* O componente usa o OpenBao (root, component)? O cofre só no despachante e na operação. */}}
{{- define "rapa.usesBao" -}}
{{- $v := .root.Values -}}
{{- if or (eq $v.keys.mode "openbao") (and (has .component (list "despachante" "operacao")) (eq $v.vault.mode "openbao")) -}}
true
{{- end -}}
{{- end -}}

{{/*
Variáveis comuns aos três componentes (root, component, baoRole): banco da aplicação,
armazenamento, chaves, observabilidade, evidências e as do OpenBao.
*/}}
{{- define "rapa.commonEnv" -}}
{{- $v := .root.Values -}}
{{- include "rapa.databaseEnv" (dict "root" .root "role" "app" "var" "RAPA_DATABASE_URL") }}
- name: RAPA_DEV
  value: {{ ternary "1" "0" $v.auth.dev.enabled | quote }}
- name: RAPA_LOG_JSON
  value: {{ ternary "1" "0" $v.observability.logJson | quote }}
- name: RAPA_METRICAS_PORTA
  value: {{ $v.observability.metricsPort | quote }}
- name: RAPA_METRICAS_POR_EMPRESA
  value: {{ ternary "1" "0" $v.observability.metricsPerCompany | quote }}
- name: OTEL_SERVICE_NAME
  value: {{ include "rapa.component" . | quote }}
{{- if $v.observability.otlpEndpoint }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ $v.observability.otlpEndpoint | quote }}
{{- end }}
{{- if $v.observability.glitchtip.existingSecret }}
{{ include "rapa.secretEnv" (dict "name" "RAPA_GLITCHTIP_DSN" "secret" $v.observability.glitchtip.existingSecret "key" $v.observability.glitchtip.key) }}
{{- end }}
- name: RAPA_ARMAZENAMENTO
  value: {{ $v.storage.mode | quote }}
{{- if eq $v.storage.mode "s3" }}
- name: RAPA_S3_ENDERECO
  value: {{ required "storage.s3.endpoint é obrigatório com storage.mode=s3" $v.storage.s3.endpoint | quote }}
- name: RAPA_S3_REGIAO
  value: {{ $v.storage.s3.region | quote }}
- name: RAPA_S3_BUCKET
  value: {{ required "storage.s3.bucket é obrigatório com storage.mode=s3" $v.storage.s3.bucket | quote }}
- name: RAPA_S3_ESTILO_CAMINHO
  value: {{ ternary "1" "0" $v.storage.s3.pathStyle | quote }}
{{- if $v.storage.s3.caSecret.name }}
- name: RAPA_S3_CA
  value: {{ printf "/var/run/rapa/s3/%s" $v.storage.s3.caSecret.key | quote }}
{{- end }}
{{ include "rapa.secretEnv" (dict "name" "AWS_ACCESS_KEY_ID" "secret" (required "storage.s3.existingSecret é obrigatório com storage.mode=s3" $v.storage.s3.existingSecret) "key" $v.storage.s3.accessKeyKey) }}
{{ include "rapa.secretEnv" (dict "name" "AWS_SECRET_ACCESS_KEY" "secret" $v.storage.s3.existingSecret "key" $v.storage.s3.secretKeyKey) }}
{{- end }}
- name: RAPA_CHAVES
  value: {{ $v.keys.mode | quote }}
{{- if eq $v.keys.mode "local" }}
{{ include "rapa.secretEnv" (dict "name" "RAPA_CHAVE_ARTEFATOS" "secret" (required "keys.localKeySecret.name é obrigatório com keys.mode=local" $v.keys.localKeySecret.name) "key" $v.keys.localKeySecret.key) }}
{{- end }}
{{- if include "rapa.usesBao" . }}
- name: RAPA_BAO_ENDERECO
  value: {{ required "vault.address é obrigatório com o OpenBao" $v.vault.address | quote }}
{{- if $v.vault.caSecret.name }}
- name: RAPA_BAO_CA
  value: {{ printf "/var/run/rapa/openbao/%s" $v.vault.caSecret.key | quote }}
{{- end }}
- name: RAPA_BAO_KV
  value: {{ $v.vault.kvMount | quote }}
- name: RAPA_BAO_TRANSIT
  value: {{ $v.vault.transitMount | quote }}
- name: RAPA_BAO_AUTH
  value: {{ $v.vault.authMethod | quote }}
- name: RAPA_BAO_PAPEL
  value: {{ .baoRole | quote }}
- name: RAPA_BAO_TOKEN_SA
  value: {{ include "rapa.baoTokenPath" . | quote }}
{{- end }}
- name: RAPA_VIDEO_EM_FALHAS
  value: {{ ternary "1" "0" $v.evidence.videoOnFailure | quote }}
- name: RAPA_RETENCAO_SUCESSO_DIAS
  value: {{ $v.evidence.retentionSuccessDays | quote }}
- name: RAPA_RETENCAO_FALHAS_DIAS
  value: {{ $v.evidence.retentionFailureDays | quote }}
- name: RAPA_RETENCAO_EVENTOS_DIAS
  value: {{ $v.evidence.retentionEventsDays | quote }}
- name: RAPA_A1_AVISO_DIAS
  value: {{ $v.a1WarnDays | quote }}
{{- end -}}

{{/* Variáveis do HTTP público (web e api): tokens, origens, proxy do gateway, hub e webhooks. */}}
{{- define "rapa.httpEnv" -}}
{{- $v := .root.Values -}}
{{- if and $v.auth.dev.enabled $v.auth.dev.tokensSecret }}
- name: RAPA_TOKENS_DEV
  value: /var/run/rapa/dev/tokens/tokens.json
{{- end }}
{{- if $v.publicUrl }}
- name: RAPA_PUBLIC_URL
  value: {{ $v.publicUrl | quote }}
{{- end }}
{{- if $v.auth.jwt.issuer }}
- name: RAPA_JWT_ISSUER
  value: {{ $v.auth.jwt.issuer | quote }}
- name: RAPA_JWT_AUDIENCE
  value: {{ $v.auth.jwt.audience | quote }}
- name: RAPA_JWKS_URL
  value: {{ $v.auth.jwt.jwksUrl | quote }}
{{- end }}
{{- with $v.auth.organizationAliases }}
- name: RAPA_ORGANIZATION_ALIASES
  value: {{ toJson . | quote }}
{{- end }}
- name: RAPA_COOKIE_SEGURO
  value: {{ ternary "1" "0" $v.auth.cookieSecure | quote }}
{{- with $v.auth.mcpOrigins }}
- name: RAPA_MCP_ORIGENS
  value: {{ join "," . | quote }}
{{- end }}
{{- with $v.auth.liveOrigins }}
- name: RAPA_ORIGENS_AO_VIVO
  value: {{ join "," . | quote }}
{{- end }}
- name: RAPA_LIMITE_POR_SEGUNDO
  value: {{ $v.auth.rateLimitPerSecond | quote }}
{{- with $v.gateway.trustedNetworks }}
- name: FORWARDED_ALLOW_IPS
  value: {{ join "," . | quote }}
{{- end }}
{{- if $v.modelHub.baseUrl }}
- name: RAPA_LLM_BASE_URL
  value: {{ $v.modelHub.baseUrl | quote }}
- name: RAPA_LLM_MODEL
  value: {{ required "modelHub.model é obrigatório com o hub do modelo" $v.modelHub.model | quote }}
- name: RAPA_LLM_CAPACIDADES
  value: /var/run/rapa/hub/capacidades.json
{{- if $v.modelHub.existingSecret }}
{{ include "rapa.secretEnv" (dict "name" "RAPA_LLM_API_KEY" "secret" $v.modelHub.existingSecret "key" $v.modelHub.apiKeyKey) }}
{{- end }}
{{- end }}
{{- if $v.publishing.webhookKey.existingSecret }}
{{ include "rapa.secretEnv" (dict "name" "RAPA_CHAVE_WEBHOOKS" "secret" $v.publishing.webhookKey.existingSecret "key" $v.publishing.webhookKey.key) }}
{{- end }}
- name: RAPA_AGENDAS_POR_EMPRESA
  value: {{ $v.publishing.schedulesPerCompany | quote }}
- name: RAPA_WEBHOOKS_POR_SEGUNDO
  value: {{ $v.publishing.webhookLookupsPerSecond | quote }}
- name: RAPA_LOTE_MAX_LINHAS
  value: {{ $v.publishing.batchMaxRows | quote }}
- name: RAPA_LOTE_MAX_BYTES
  value: {{ $v.publishing.batchMaxBytes | int64 | quote }}
- name: RAPA_CONSERTO_CANDIDATOS
  value: {{ $v.repairCandidates | quote }}
{{- end -}}

{{/* Variáveis dos pods de uso único (despachante e web): imagem, namespace, limites e saída. */}}
{{- define "rapa.podsEnv" -}}
{{- $v := .root.Values -}}
{{- $e := $v.executions -}}
- name: RAPA_K8S_NAMESPACE_EXECUCOES
  value: {{ $e.namespace | quote }}
- name: RAPA_K8S_IMAGEM_EXECUTOR
  value: {{ include "rapa.executorImage" .root | quote }}
- name: RAPA_K8S_RUNTIME_CLASS
  value: {{ $e.runtimeClass | quote }}
- name: RAPA_K8S_CPU
  value: {{ $e.cpu | quote }}
- name: RAPA_K8S_MEMORIA
  value: {{ $e.memory | quote }}
- name: RAPA_K8S_SA_EXECUTOR
  value: {{ include "rapa.executorServiceAccount" .root | quote }}
{{- with $v.images.pullSecrets }}
- name: RAPA_K8S_PULL_SECRET
  value: {{ first . | quote }}
{{- end }}
{{- with $e.nodeSelector }}
- name: RAPA_K8S_NODE_SELECTOR
  value: {{ toJson . | quote }}
{{- end }}
{{- with $e.tolerations }}
- name: RAPA_K8S_TOLERATIONS
  value: {{ toJson . | quote }}
{{- end }}
{{- if $e.egress.proxy.existingSecret }}
{{ include "rapa.secretEnv" (dict "name" "RAPA_PROXY_SAIDA" "secret" $e.egress.proxy.existingSecret "key" $e.egress.proxy.key) }}
{{- else if $e.egress.proxy.url }}
- name: RAPA_PROXY_SAIDA
  value: {{ $e.egress.proxy.url | quote }}
{{- end }}
- name: RAPA_REDES_INTERNAS_PERMITIDAS
  value: {{ join "," $e.egress.allowedInternalNetworks | quote }}
{{- end -}}

{{- define "rapa.executorServiceAccount" -}}
{{- .Values.executions.serviceAccount -}}
{{- end -}}

{{/* Volumes comuns (root, component): /tmp, CA do banco, OpenBao, S3, dev. */}}
{{- define "rapa.commonVolumes" -}}
{{- $v := .root.Values -}}
- name: tmp
  emptyDir:
    sizeLimit: 1Gi
{{- include "rapa.databaseVolume" .root }}
{{- if include "rapa.usesBao" . }}
- name: bao-token
  projected:
    defaultMode: 0440
    sources:
      - serviceAccountToken:
          audience: {{ $v.vault.tokenAudience }}
          expirationSeconds: {{ $v.vault.tokenExpirationSeconds }}
          path: token
{{- if $v.vault.caSecret.name }}
- name: openbao-ca
  secret:
    secretName: {{ $v.vault.caSecret.name }}
    defaultMode: 0444
    items:
      - key: {{ $v.vault.caSecret.key }}
        path: {{ $v.vault.caSecret.key }}
{{- end }}
{{- end }}
{{- if and (eq $v.storage.mode "s3") $v.storage.s3.caSecret.name }}
- name: s3-ca
  secret:
    secretName: {{ $v.storage.s3.caSecret.name }}
    defaultMode: 0444
    items:
      - key: {{ $v.storage.s3.caSecret.key }}
        path: {{ $v.storage.s3.caSecret.key }}
{{- end }}
{{- if and .http $v.auth.dev.enabled $v.auth.dev.tokensSecret }}
- name: dev-tokens
  secret:
    secretName: {{ $v.auth.dev.tokensSecret }}
    defaultMode: 0440
    items:
      - key: tokens.json
        path: tokens.json
{{- end }}
{{- end -}}

{{- define "rapa.commonMounts" -}}
{{- $v := .root.Values -}}
- name: tmp
  mountPath: /tmp
{{- include "rapa.databaseMount" .root }}
{{- if include "rapa.usesBao" . }}
- name: bao-token
  mountPath: /var/run/secrets/rapa/bao
  readOnly: true
{{- if $v.vault.caSecret.name }}
- name: openbao-ca
  mountPath: /var/run/rapa/openbao
  readOnly: true
{{- end }}
{{- end }}
{{- if and (eq $v.storage.mode "s3") $v.storage.s3.caSecret.name }}
- name: s3-ca
  mountPath: /var/run/rapa/s3
  readOnly: true
{{- end }}
{{- if and .http $v.auth.dev.enabled $v.auth.dev.tokensSecret }}
- name: dev-tokens
  mountPath: /var/run/rapa/dev/tokens
  readOnly: true
{{- end }}
{{- end -}}

{{/*
Redes internas que os pods de execução e de sessão nunca alcançam (além das de
executions.egress.extraInternalNetworks). Fixas no chart: nenhum valor as tira.
*/}}
{{- define "rapa.internalIPv4" -}}
- 0.0.0.0/8
- 10.0.0.0/8
- 100.64.0.0/10
- 127.0.0.0/8
- 169.254.0.0/16
- 172.16.0.0/12
- 192.168.0.0/16
{{- end -}}

{{/*
As equivalentes em IPv6: loopback, não especificado e compatível com IPv4 (::/96), as redes
internas de IPv4 dentro do prefixo NAT64 64:ff9b::/96 (o resto dele é a
internet de IPv4 pelo NAT64), o NAT64 de uso local, o 6to4, as locais únicas (fc00::/7, onde
fica o metadata da AWS em IPv6), as link-local e o multicast. O IPv4 mapeado (::ffff:0:0/96)
não entra: ele nunca sai como IPv6 (o kernel usa o IPv4, que as regras de IPv4 já cobrem), e
a API do Kubernetes o lê como IPv4 e recusa a política ("must be a strict subset of cidr").
*/}}
{{- define "rapa.internalIPv6" -}}
- ::/96
- 64:ff9b::/104
- 64:ff9b::a00:0/104
- 64:ff9b::6440:0/106
- 64:ff9b::7f00:0/104
- 64:ff9b::a9fe:0/112
- 64:ff9b::ac10:0/108
- 64:ff9b::c0a8:0/112
- 64:ff9b:1::/48
- 2002::/16
- fc00::/7
- fe80::/10
- ff00::/8
{{- end -}}

{{/* Uma lista de portas TCP de NetworkPolicy. */}}
{{- define "rapa.tcpPorts" -}}
{{- range . }}
- protocol: TCP
  port: {{ . }}
{{- end }}
{{- end -}}

{{/* A regra de DNS das NetworkPolicies. */}}
{{- define "rapa.dnsEgress" -}}
- to:
    {{- toYaml .Values.network.dns | nindent 4 }}
  ports:
    - protocol: UDP
      port: 53
    - protocol: TCP
      port: 53
{{- end -}}

{{/*
Os rótulos do pod da operação (criado à mão a partir do PodTemplate): sem
app.kubernetes.io/instance, que o Argo CD usa para achar os recursos de uma Application.
*/}}
{{- define "rapa.operationLabels" -}}
app.kubernetes.io/name: rapa
app.kubernetes.io/part-of: rapa
app.kubernetes.io/component: operacao
{{- end -}}

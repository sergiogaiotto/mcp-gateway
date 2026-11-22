{{/*
O Deployment de um componente do plano de controle. Parâmetros:
  root       o contexto do chart
  component  web, api ou despachante
  values     .Values.<componente>
  port       porta do serviço (--port: a do HTTP ou, no despachante, a da saúde)
  portName   http ou saude (os probes usam esta)
  extraPorts outras portas do contêiner (lista de name e port; no despachante, o canal)
  http       o componente serve o HTTP público (web e api)
  kubeApi    o componente fala com a API do Kubernetes (cria os pods de uso único)
  baoRole    papel do OpenBao do componente
  env        variáveis próprias do componente (YAML)
  volumes    volumes próprios (YAML)
  mounts     montagens próprias (YAML)
  hpa        o HPA controla as réplicas
*/}}
{{- define "rapa.deployment" -}}
{{- $root := .root -}}
{{- $v := $root.Values -}}
{{- $c := .values -}}
{{- $sel := dict "root" $root "component" .component -}}
{{- $ctx := dict "root" $root "component" .component "baoRole" .baoRole "http" .http -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "rapa.component" $sel }}
  namespace: {{ $root.Release.Namespace }}
  labels:
    {{- include "rapa.componentLabels" $sel | nindent 4 }}
spec:
  {{- if not .hpa }}
  replicas: {{ $c.replicas }}
  {{- end }}
  revisionHistoryLimit: 5
  selector:
    matchLabels:
      {{- include "rapa.selectorLabels" $sel | nindent 6 }}
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1
  template:
    metadata:
      labels:
        {{- include "rapa.componentLabels" $sel | nindent 8 }}
      annotations:
        {{- with $c.podAnnotations }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
        rapa.dev/capacidades: {{ $v.modelHub.capabilities | sha256sum | trunc 16 | quote }}
    spec:
      serviceAccountName: {{ include "rapa.component" $sel }}
      automountServiceAccountToken: {{ .kubeApi }}
      enableServiceLinks: false
      terminationGracePeriodSeconds: {{ $c.terminationGracePeriodSeconds }}
      securityContext:
        {{- include "rapa.podSecurityContext" $root | nindent 8 }}
      {{- include "rapa.imagePullSecrets" $root | nindent 6 }}
      {{- include "rapa.topologySpread" $sel | nindent 6 }}
      {{- with $c.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $c.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $c.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
        - name: {{ .component }}
          image: {{ include "rapa.controlImage" $root | quote }}
          imagePullPolicy: {{ $v.images.control.pullPolicy }}
          args:
            - python
            - -m
            - rapa.server
            - --host
            - {{ $v.network.listenAddress | quote }}
            - --port
            - {{ .port | quote }}
          ports:
            - name: {{ .portName }}
              containerPort: {{ .port }}
              protocol: TCP
            {{- range .extraPorts }}
            - name: {{ .name }}
              containerPort: {{ .port }}
              protocol: TCP
            {{- end }}
            {{- if ne (int $v.observability.metricsPort) 0 }}
            - name: metricas
              containerPort: {{ $v.observability.metricsPort }}
              protocol: TCP
            {{- end }}
          env:
            {{- include "rapa.commonEnv" $ctx | nindent 12 }}
            {{- if .http }}
            {{- include "rapa.httpEnv" $ctx | nindent 12 }}
            {{- end }}
            {{- .env | nindent 12 }}
          startupProbe:
            httpGet:
              path: /saude
              port: {{ .portName }}
            periodSeconds: 5
            failureThreshold: 30
            timeoutSeconds: 3
          livenessProbe:
            httpGet:
              path: /saude
              port: {{ .portName }}
            periodSeconds: 30
            failureThreshold: {{ ternary 3 10 .http }}
            timeoutSeconds: 5
          readinessProbe:
            httpGet:
              path: /saude/pronto
              port: {{ .portName }}
            periodSeconds: 10
            failureThreshold: 3
            timeoutSeconds: 5
          lifecycle:
            preStop:
              sleep:
                seconds: 5
          resources:
            {{- toYaml $c.resources | nindent 12 }}
          securityContext:
            {{- include "rapa.containerSecurityContext" $root | nindent 12 }}
          volumeMounts:
            {{- include "rapa.commonMounts" $ctx | nindent 12 }}
            {{- with .mounts }}
            {{- . | nindent 12 }}
            {{- end }}
      volumes:
        {{- include "rapa.commonVolumes" $ctx | nindent 8 }}
        {{- with .volumes }}
        {{- . | nindent 8 }}
        {{- end }}
{{- end -}}

{{/* O volume e a montagem da matriz da sonda do hub (web e api). */}}
{{- define "rapa.hubVolume" -}}
{{- if and .Values.modelHub.baseUrl .Values.modelHub.capabilities }}
- name: hub-capacidades
  configMap:
    name: {{ include "rapa.name" . }}-hub-capacidades
    defaultMode: 0444
{{- end }}
{{- end -}}

{{- define "rapa.hubMount" -}}
{{- if and .Values.modelHub.baseUrl .Values.modelHub.capabilities }}
- name: hub-capacidades
  mountPath: /var/run/rapa/hub
  readOnly: true
{{- end }}
{{- end -}}

{{/* O PodDisruptionBudget de um componente (root, component, values). */}}
{{- define "rapa.pdb" -}}
{{- $sel := dict "root" .root "component" .component -}}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "rapa.component" $sel }}
  namespace: {{ .root.Release.Namespace }}
  labels:
    {{- include "rapa.componentLabels" $sel | nindent 4 }}
spec:
  maxUnavailable: {{ .values.pdb.maxUnavailable }}
  selector:
    matchLabels:
      {{- include "rapa.selectorLabels" $sel | nindent 6 }}
{{- end -}}

{{/* A conta de serviço de um componente (root, component, automount). */}}
{{- define "rapa.serviceAccount" -}}
{{- $sel := dict "root" .root "component" .component -}}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "rapa.component" $sel }}
  namespace: {{ .root.Release.Namespace }}
  labels:
    {{- include "rapa.componentLabels" $sel | nindent 4 }}
automountServiceAccountToken: {{ .automount }}
{{- end -}}

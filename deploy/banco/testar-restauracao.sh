#!/usr/bin/env bash
# O teste de restauração semanal do banco (docs/OPERACAO.md, "Backup e restauração"): sobe um
# Cluster novo do CloudNativePG a partir dos backups do Barman Cloud (o último backup de base e
# o WAL até agora, ou até ALVO_HORA), num namespace à parte, confere que o banco abre e que os
# dados estão lá, e apaga tudo. O banco de produção não é tocado: o Cluster restaurado só lê o
# caminho dos backups e não arquiva WAL.
#
# Uso: deploy/banco/testar-restauracao.sh
#   ORIGEM_NS (rapa), NS (rapa-restauracao), CLUSTER (rapa-banco), BANCO (rapa),
#   DONO (rapa_owner, o dono do banco que está nos backups), TAMANHO (50Gi),
#   ALVO_HORA (opcional, "2026-11-20 03:00:00+00"), MANTER=1 (não apaga no fim).
#
# Toda semana, de uma máquina da operação com o kubectl do cluster (docs/OPERACAO.md 1.5):
#
#   17 6 * * 1  /caminho/do/rapa/deploy/banco/testar-restauracao.sh >>/var/log/rapa-restauracao.log 2>&1
#
# A última linha diz o que foi conferido e quanto tempo levou (o tempo de recuperação); uma
# saída diferente de 0 é uma restauração que falhou e precisa de alguém.
set -euo pipefail

origem_ns="${ORIGEM_NS:-rapa}"
ns="${NS:-rapa-restauracao}"
cluster="${CLUSTER:-rapa-banco}"
banco="${BANCO:-rapa}"
dono="${DONO:-rapa_owner}"
tamanho="${TAMANHO:-50Gi}"
inicio="$(date +%s)"
restaurado="${cluster}-restaurado"
objeto="${cluster}-backup"

limpar() {
  if [[ "${MANTER:-0}" != "1" ]]; then
    kubectl delete namespace "$ns" --wait=false >/dev/null 2>&1 || true
  fi
}
trap limpar EXIT

# Copia um recurso para o namespace do teste, sem os metadados da origem (só leitura nela).
copiar() {
  kubectl -n "$origem_ns" get "$1" "$2" -o json | python3 -c '
import json, sys
doc = json.load(sys.stdin)
meta = doc["metadata"]
doc["metadata"] = {"name": meta["name"], "namespace": sys.argv[1]}
doc.pop("status", None)
print(json.dumps(doc))
' "$ns" | kubectl apply -f - >/dev/null
}

kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
segredo="$(kubectl -n "$origem_ns" get objectstore "$objeto" \
  -o jsonpath='{.spec.configuration.s3Credentials.accessKeyId.name}')"
copiar secret "$segredo"
ca="$(kubectl -n "$origem_ns" get objectstore "$objeto" -o jsonpath='{.spec.configuration.endpointCA.name}')"
if [[ -n "$ca" ]]; then
  copiar secret "$ca"
fi
copiar objectstore "$objeto"

alvo=""
if [[ -n "${ALVO_HORA:-}" ]]; then
  alvo="      recoveryTarget:
        targetTime: \"${ALVO_HORA}\""
fi
kubectl -n "$ns" apply -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: ${restaurado}
spec:
  instances: 1
  enableSuperuserAccess: false
  storage:
    size: ${tamanho}
  bootstrap:
    recovery:
      source: origem
      database: ${banco}
      # O dono que está nos backups: sem ele, o operador assume um papel com o nome do banco,
      # que não existe ali, e o Cluster restaurado não fica pronto.
      owner: ${dono}
${alvo}
  externalClusters:
    - name: origem
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: ${objeto}
          serverName: ${cluster}
EOF

kubectl -n "$ns" wait "cluster/${restaurado}" --for=condition=Ready --timeout=60m
consulta() {
  kubectl -n "$ns" exec "${restaurado}-1" -c postgres -- psql -d "$banco" -At -c "$1"
}
empresas="$(consulta 'SELECT count(*) FROM companies')"
execucoes="$(consulta 'SELECT count(*) FROM runs')"
ultima="$(consulta 'SELECT coalesce(max(created_at)::text, '"'"'nenhuma'"'"') FROM runs')"
revisao="$(consulta 'SELECT version_num FROM alembic_version')"
if [[ "$revisao" == "" ]]; then
  echo "o banco restaurado não tem a tabela de migrações" >&2
  exit 1
fi
echo "restauração conferida em $(( $(date +%s) - inicio ))s: revisão ${revisao}, ${empresas} empresas, ${execucoes} execuções (a mais nova: ${ultima})"

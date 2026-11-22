#!/usr/bin/env bash
# Prepara o OpenBao para o RaPA: o KV v2, o transit, o método kubernetes, as políticas e
# os papéis do despachante, da interface e API e da operação.
#
# Idempotente: rodar de novo não muda o que já está certo (monta só o que falta e
# reescreve políticas e papéis com o mesmo conteúdo). Nenhum segredo aqui: quem prepara
# entra antes no OpenBao (BAO_TOKEN no ambiente, ou "bao login"), e o RaPA nunca recebe
# esse token.
#
# Uso (de uma máquina com o CLI "bao" e acesso ao OpenBao):
#
#   export BAO_ADDR=https://openbao.cofre.svc:8200 BAO_CACERT=/caminho/do/ca.pem
#   bao login -method=oidc   # ou outro método de quem administra o cofre
#   ./deploy/openbao/preparar.sh
#
# Variáveis (todas opcionais, com os padrões do RaPA):
#   RAPA_BAO_KV         montagem do KV v2 (padrão: rapa)
#   RAPA_BAO_TRANSIT    montagem do transit (padrão: rapa-transit)
#   RAPA_BAO_AUTH       montagem do método kubernetes (padrão: kubernetes)
#   RAPA_NAMESPACE      namespace do plano de controle (padrão: rapa)
#   RAPA_BAO_AUDIENCIA  audiência do token projetado das contas de serviço (padrão: openbao)
#   RAPA_BAO_TTL        validade dos tokens do OpenBao dos papéis (padrão: 1h)
#   RAPA_SA_DESPACHANTE contas de serviço do despachante (padrão: rapa-despachante)
#   RAPA_SA_API         contas de serviço da interface e da API (padrão: rapa-web,rapa-api)
#   RAPA_SA_OPERACAO    conta de serviço dos Jobs da operação (padrão: rapa-operacao)
#   KUBERNETES_HOST     API do Kubernetes vista pelo OpenBao
#                       (padrão: https://kubernetes.default.svc)
set -euo pipefail

: "${BAO_ADDR:?defina BAO_ADDR com o endereço do OpenBao}"
command -v bao >/dev/null || { echo "preparar.sh: o CLI 'bao' não está no PATH" >&2; exit 1; }

KV="${RAPA_BAO_KV:-rapa}"
TRANSIT="${RAPA_BAO_TRANSIT:-rapa-transit}"
AUTH="${RAPA_BAO_AUTH:-kubernetes}"
NAMESPACE="${RAPA_NAMESPACE:-rapa}"
AUDIENCE="${RAPA_BAO_AUDIENCIA:-openbao}"
TTL="${RAPA_BAO_TTL:-1h}"
SA_DISPATCHER="${RAPA_SA_DESPACHANTE:-rapa-despachante}"
SA_API="${RAPA_SA_API:-rapa-web,rapa-api}"
SA_OPERATION="${RAPA_SA_OPERACAO:-rapa-operacao}"
K8S_HOST="${KUBERNETES_HOST:-https://kubernetes.default.svc}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Os nomes vão para caminhos do OpenBao: só letras, números, _ e - (e / nas montagens).
for value in "$KV" "$TRANSIT" "$AUTH"; do
  [[ "$value" =~ ^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$ ]] || {
    echo "preparar.sh: montagem inválida: $value" >&2
    exit 1
  }
done
for value in "$NAMESPACE" "$AUDIENCE" "$SA_DISPATCHER" "$SA_API" "$SA_OPERATION" "$TTL"; do
  [[ "$value" =~ ^[A-Za-z0-9_.,-]+$ ]] || {
    echo "preparar.sh: valor inválido: $value" >&2
    exit 1
  }
done

mounted() {
  # $1: "secrets" ou "auth"; $2: a montagem. Confere pelo JSON da listagem.
  bao "$1" list -format=json | grep -q "\"$2/\""
}

echo "KV v2 em $KV/"
if ! mounted secrets "$KV"; then
  bao secrets enable -path="$KV" -version=2 kv
fi

echo "transit em $TRANSIT/"
if ! mounted secrets "$TRANSIT"; then
  bao secrets enable -path="$TRANSIT" transit
fi

echo "método kubernetes em auth/$AUTH/"
if ! mounted auth "$AUTH"; then
  bao auth enable -path="$AUTH" kubernetes
fi
# Rodando no cluster, o OpenBao confere os tokens com a própria conta de serviço (que
# precisa do papel system:auth-delegator) e o CA montado no pod dele.
bao write "auth/$AUTH/config" kubernetes_host="$K8S_HOST" >/dev/null

policy() {
  # As políticas usam as montagens padrão; aqui elas trocam para as configuradas.
  sed -e "s#^path \"rapa/#path \"$KV/#" -e "s#^path \"rapa-transit/#path \"$TRANSIT/#" \
    "$HERE/politicas/$1.hcl" | bao policy write "$1" - >/dev/null
  echo "política $1"
}
policy rapa-despachante
policy rapa-api
policy rapa-operacao

role() {
  # $1: papel e política; $2: contas de serviço.
  bao write "auth/$AUTH/role/$1" \
    bound_service_account_names="$2" \
    bound_service_account_namespaces="$NAMESPACE" \
    audience="$AUDIENCE" \
    token_policies="$1" \
    token_ttl="$TTL" \
    token_max_ttl=24h \
    token_no_default_policy=true >/dev/null
  echo "papel $1 (contas: $2; namespace: $NAMESPACE)"
}
role rapa-despachante "$SA_DISPATCHER"
role rapa-api "$SA_API"
role rapa-operacao "$SA_OPERATION"

echo "pronto: configure RAPA_BAO_PAPEL com o papel de cada componente"

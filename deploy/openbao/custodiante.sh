#!/usr/bin/env bash
# A política do custodiante de uma empresa no OpenBao (docs/OPERACAO.md 1.6): quem grava os
# segredos das credenciais dela, direto no cofre, só debaixo de empresas/<uuid>/ do KV.
# Grava e lista; nunca lê o valor de um segredo (quem lê é o despachante), nunca apaga e
# nunca alcança outra empresa. Uma política por empresa: nunca uma de empresas/*, que deixaria
# o custodiante de uma empresa trocar os segredos de outra.
#
# Uso (quem administra o cofre, já com "bao login"):
#
#   ./deploy/openbao/custodiante.sh <uuid da empresa>
#
# Escreve a política rapa-custodiante-<uuid> (idempotente). Depois, ligue-a só à identidade
# do custodiante daquela empresa no método de entrada do cofre (um grupo do OIDC, um
# usuário do userpass): deploy/openbao/README.md, "Custodiantes".
#
# Variáveis (opcionais): RAPA_BAO_KV, a montagem do KV v2 (padrão: rapa).
set -euo pipefail

: "${BAO_ADDR:?defina BAO_ADDR com o endereço do OpenBao}"
command -v bao >/dev/null || { echo "custodiante.sh: o CLI 'bao' não está no PATH" >&2; exit 1; }

KV="${RAPA_BAO_KV:-rapa}"
COMPANY="${1:-}"

[[ "$KV" =~ ^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$ ]] || {
  echo "custodiante.sh: montagem inválida: $KV" >&2
  exit 1
}
# O id da empresa como o RaPA o põe no caminho (o UUID em minúsculas): nada que mude o caminho.
[[ "$COMPANY" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || {
  echo "custodiante.sh: informe o id da empresa (o UUID, em minúsculas)" >&2
  exit 1
}

bao policy write "rapa-custodiante-$COMPANY" - >/dev/null <<EOF
# O custodiante da empresa $COMPANY: grava os segredos dela e lista os caminhos.
path "$KV/data/empresas/$COMPANY/*" {
  capabilities = ["create", "update"]
}

path "$KV/metadata/empresas/$COMPANY/*" {
  capabilities = ["list"]
}

# O CLI confere a versão do KV antes de gravar (bao kv put -mount=$KV).
path "sys/internal/ui/mounts/$KV" {
  capabilities = ["read"]
}
EOF
echo "política rapa-custodiante-$COMPANY (só $KV/data/empresas/$COMPANY/)"

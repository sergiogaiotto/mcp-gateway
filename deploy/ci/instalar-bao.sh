#!/usr/bin/env bash
# Instala o CLI e servidor "bao" do OpenBao (MPL-2.0) a partir do pacote oficial da release,
# com a versão fixa e o SHA-256 conferido antes de abrir o pacote (o mesmo do
# checksums-linux.txt da release). Usado no CI: no job de testes (o teste contra o
# "bao server -dev", que lá não pode ser pulado) e no job kind (o preparar.sh roda no runner,
# contra o OpenBao do cluster pelo port-forward).
#
# Uso: deploy/ci/instalar-bao.sh [pasta de destino]   (padrão: /usr/local/bin)
set -euo pipefail

versao=2.5.5
pacote="bao_${versao}_Linux_x86_64.tar.gz"
sha256=2c5577707e97fc95c2086950f39880ead5e45b356c94388e5cb606f5a5c2b697
destino="${1:-/usr/local/bin}"

[[ "$(uname -s)-$(uname -m)" == "Linux-x86_64" ]] || {
  echo "instalar-bao.sh: só Linux x86_64 (o pacote fixado)" >&2
  exit 1
}
trabalho="$(mktemp -d)"
trap 'rm -rf "$trabalho"' EXIT
curl -fsSL --retry 3 --proto '=https' --tlsv1.2 -o "$trabalho/$pacote" \
  "https://github.com/openbao/openbao/releases/download/v${versao}/${pacote}"
echo "${sha256}  $trabalho/$pacote" | sha256sum --check --strict -
mkdir -p "$trabalho/pacote"
tar -xzf "$trabalho/$pacote" -C "$trabalho/pacote" bao
install -d "$destino"
install -m 0755 "$trabalho/pacote/bao" "$destino/bao"
"$destino/bao" version

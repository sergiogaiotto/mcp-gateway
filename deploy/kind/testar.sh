#!/usr/bin/env bash
# O ponta a ponta do job kind: abre os port-forwards (API do RaPA, OpenBao e o filer do
# SeaweedFS) e roda deploy/kind/ponta_a_ponta.py (publica e executa um robô pela REST, espera
# succeeded, lê o artefato pela API e confere que no S3 ele só existe cifrado).
set -euo pipefail

raiz="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
token_admin="${TOKEN_ADMIN:-token-admin-kind}"
token_criadora="${TOKEN_CRIADORA:-token-criadora-kind}"
pids=()
trap 'kill "${pids[@]}" 2>/dev/null || true' EXIT

encaminhar() {
  kubectl -n "$1" port-forward "$2" "$3" >/dev/null 2>&1 &
  pids+=("$!")
}
encaminhar rapa svc/rapa-api 18000:8000
encaminhar cofre svc/openbao 18200:8200
encaminhar armazenamento svc/seaweedfs-filer 18888:8888
for porta in 18000 18200 18888; do
  for _ in $(seq 1 60); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$porta") 2>/dev/null; then
      break
    fi
    sleep 1
  done
done

cd "$raiz"
uv run --frozen python deploy/kind/ponta_a_ponta.py \
  --api http://127.0.0.1:18000 --bao http://127.0.0.1:18200 --filer http://127.0.0.1:18888 \
  --portal http://portal.rapa.test:8080 \
  --token-admin "$token_admin" --token-criadora "$token_criadora"

#!/usr/bin/env bash
# A imagem do executor rodando o worker de verdade contra o portal de teste (job imagens):
# o portal sobe da imagem de controle numa rede do Docker (com o nome portal-v1.rapa.test), o
# trabalho de teste (deploy/ci/trabalho_de_teste.py) entra no portal com login e TOTP e baixa
# as notas, e o resultado é conferido. O executor roda como no pod: usuário 10001, disco só de
# leitura, sem capacidades, com o Chromium e os argumentos da própria imagem.
#
# Uso: deploy/ci/testar-executor.sh rapa-executor:ci rapa-controle:ci
set -euo pipefail

executor="$1"
controle="$2"
raiz="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
rede=rapa-ci
trabalho="$(mktemp -d)"
trap 'docker logs portal-ci >"${LOGS:-/tmp}/portal.log" 2>&1 || true; docker rm -f portal-ci >/dev/null 2>&1 || true; docker network rm "$rede" >/dev/null 2>&1 || true; sudo rm -rf "$trabalho"' EXIT

docker network create "$rede" >/dev/null
docker run -d --name portal-ci --network "$rede" --network-alias portal-v1.rapa.test \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges "$controle" \
  python -m uvicorn --factory rapa.fixtures.portal:create_app --host 0.0.0.0 --port 8080 >/dev/null

cd "$raiz"
uv run --frozen python deploy/ci/trabalho_de_teste.py preparar \
  --portal http://portal-v1.rapa.test:8080 --pasta "$trabalho"
sudo chown -R 10001:10001 "$trabalho"

for _ in $(seq 1 30); do
  if docker run --rm --network "$rede" "$controle" python -c \
    'import urllib.request; urllib.request.urlopen("http://portal-v1.rapa.test:8080/login", timeout=2)' \
    >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

docker run --rm --network "$rede" --read-only --tmpfs /tmp:rw,size=512m --shm-size 256m \
  --cap-drop ALL --security-opt no-new-privileges -v "$trabalho:/trabalho" "$executor" \
  sh -c 'exec python -m rapa.runner.worker --job /trabalho/job.json \
    --secrets /trabalho/secrets.json --result /trabalho/result.json \
    --events /trabalho/events.jsonl --artifacts /trabalho/artefatos \
    --chromium "$RAPA_CHROMIUM_PATH" --browser-arg="$RAPA_ARGS_NAVEGADOR"'

sudo chown -R "$(id -u):$(id -g)" "$trabalho"
uv run --frozen python deploy/ci/trabalho_de_teste.py conferir --pasta "$trabalho"

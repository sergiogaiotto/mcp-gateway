#!/usr/bin/env bash
# A imagem de controle contra o PostgreSQL de serviço do CI (job imagens), rodando como no
# cluster: usuário 10001, disco só de leitura (/tmp num tmpfs), sem capacidades e sem ganhar
# privilégios. Cria os papéis como o operador faz, roda a migração, cria uma empresa pelo
# CLI e sobe o servidor (web, api e despachante num processo) com tokens de desenvolvimento,
# até /saude/pronto responder 200 e a REST reconhecer o token.
#
# Uso: deploy/ci/testar-controle.sh rapa-controle:ci   (PGHOST, PGPORT, PGUSER, PGPASSWORD do
# superusuário do PostgreSQL de serviço)
set -euo pipefail

imagem="$1"
pg_host="${PGHOST:-127.0.0.1}"
pg_porta="${PGPORT:-5432}"
trabalho="$(mktemp -d)"
nome=rapa-controle-ci
trap 'docker logs "$nome" >"${LOGS:-/tmp}/rapa-controle.log" 2>&1 || true; docker rm -f "$nome" >/dev/null 2>&1 || true; rm -rf "$trabalho"' EXIT

rodar() {
  docker run --rm --network host --read-only --tmpfs /tmp:rw,size=256m --cap-drop ALL \
    --security-opt no-new-privileges "$@"
}

echo "::group::Usuário, disco só de leitura e ffmpeg"
[[ "$(rodar "$imagem" id -u)" == "10001" ]]
rodar "$imagem" sh -c '! touch /opt/rapa/escrita 2>/dev/null'
rodar "$imagem" python -c 'import rapa.server, rapa.control.db.migrate, rapa.ui.app'
echo "::endgroup::"

echo "::group::Papéis e banco"
senha_dono="$(openssl rand -hex 16)"
senha_app="$(openssl rand -hex 16)"
senha_despachante="$(openssl rand -hex 16)"
psql -v ON_ERROR_STOP=1 -h "$pg_host" -p "$pg_porta" -d postgres <<SQL
CREATE ROLE rapa_owner LOGIN PASSWORD '$senha_dono' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE rapa_app LOGIN PASSWORD '$senha_app' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
CREATE ROLE rapa_dispatcher LOGIN PASSWORD '$senha_despachante' NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE;
CREATE DATABASE rapa OWNER rapa_owner;
SQL
url() { echo "postgresql://$1:$2@$pg_host:$pg_porta/rapa"; }
echo "::endgroup::"

echo "::group::Migração e empresa pelo CLI"
rodar -e RAPA_DATABASE_URL="$(url rapa_owner "$senha_dono")" "$imagem" \
  python -m rapa.control.db.migrate
empresa="$(rodar -e RAPA_DATABASE_URL="$(url rapa_app "$senha_app")" "$imagem" \
  python -m rapa.control.admin empresa criar "Empresa de teste" --organizacao org-teste \
  --admin pessoa-teste --area Fiscal --por ci | tail -n 1)"
echo "empresa: $empresa"
echo "::endgroup::"

echo "::group::Servidor com tokens de desenvolvimento"
printf '{"token-ci": {"sujeito": "pessoa-teste", "empresa": "%s"}}' "$empresa" >"$trabalho/tokens.json"
chmod 0755 "$trabalho"
chmod 0644 "$trabalho/tokens.json"
docker run -d --name "$nome" --network host --read-only --tmpfs /tmp:rw,size=512m \
  --cap-drop ALL --security-opt no-new-privileges -v "$trabalho:/config:ro" \
  -e RAPA_DEV=1 -e RAPA_TOKENS_DEV=/config/tokens.json -e RAPA_LOG_JSON=1 \
  -e RAPA_DATABASE_URL="$(url rapa_app "$senha_app")" \
  -e RAPA_DISPATCHER_DATABASE_URL="$(url rapa_dispatcher "$senha_despachante")" \
  -e RAPA_COFRE=nenhum -e RAPA_ARMAZENAMENTO=local -e RAPA_CHAVES=local \
  -e RAPA_CHAVE_ARTEFATOS="$(openssl rand -hex 32)" -e RAPA_METRICAS_PORTA=9100 \
  "$imagem" >/dev/null
pronto=""
for _ in $(seq 1 90); do
  if curl -fsS http://127.0.0.1:8000/saude/pronto >/dev/null 2>&1; then
    pronto=1
    break
  fi
  sleep 2
done
if [[ -z "$pronto" ]]; then
  echo "o servidor não ficou pronto em 3 minutos" >&2
  docker logs "$nome" >&2 || true
  exit 1
fi
curl -fsS http://127.0.0.1:8000/saude
curl -fsS -H "Authorization: Bearer token-ci" http://127.0.0.1:8000/v1/eu
# A porta de métricas só ouve no processo (nunca pela rota pública).
curl -fsS http://127.0.0.1:9100/metrics | head -n 5
echo "::endgroup::"

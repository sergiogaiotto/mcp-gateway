#!/usr/bin/env bash
# Instala o RaPA num cluster kind (o job kind do CI), na ordem da produção (docs/OPERACAO.md):
# os componentes externos (operador do CloudNativePG, OpenBao em modo dev, um SeaweedFS de um
# nó e o portal de teste), os segredos, a release do banco, o preparo do OpenBao, a release da
# aplicação (a migração roda antes, como hook), a empresa de teste pelo CLI e os tokens de
# desenvolvimento dela.
#
# Precisa de kind, kubectl, helm e openssl, e das imagens rapa-controle:ci e rapa-executor:ci
# já construídas (IMAGEM_CONTROLE e IMAGEM_EXECUTOR trocam os nomes). As versões dos charts
# externos podem ser fixadas com CNPG_CHART_VERSION e OPENBAO_CHART_VERSION.
#
# Nenhuma senha sai deste script: as do banco e as chaves do S3 são aleatórias e vão direto
# para Secrets; os tokens de desenvolvimento só valem neste cluster.
set -euo pipefail

raiz="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
chart="$raiz/deploy/helm/rapa"
kind_dir="$raiz/deploy/kind"
cluster="${KIND_CLUSTER:-rapa}"
ns=rapa
controle="${IMAGEM_CONTROLE:-rapa-controle:ci}"
executor="${IMAGEM_EXECUTOR:-rapa-executor:ci}"
token_admin="${TOKEN_ADMIN:-token-admin-kind}"
token_criadora="${TOKEN_CRIADORA:-token-criadora-kind}"
trabalho="$(mktemp -d)"
trap 'rm -rf "$trabalho"' EXIT

aleatorio() { openssl rand -hex 24; }
aplicar() { kubectl apply -f - >/dev/null; }
grupo() { echo "::group::$*"; }
fim() { echo "::endgroup::"; }

grupo "Imagens no kind"
kind load docker-image "$controle" "$executor" --name "$cluster"
fim

grupo "DNS do portal de teste (portal.rapa.test)"
kubectl -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}' >"$trabalho/Corefile"
if ! grep -q 'portal.rapa.test' "$trabalho/Corefile"; then
  sed -i 's/^\(\s*\)ready$/\1ready\n\1rewrite name exact portal.rapa.test portal.rapa-teste.svc.cluster.local/' \
    "$trabalho/Corefile"
fi
grep -q 'rewrite name exact portal.rapa.test' "$trabalho/Corefile"
kubectl -n kube-system create configmap coredns --from-file=Corefile="$trabalho/Corefile" \
  --dry-run=client -o yaml | aplicar
kubectl -n kube-system rollout restart deployment coredns
kubectl -n kube-system rollout status deployment coredns --timeout=300s
fim

grupo "Operador do CloudNativePG e OpenBao (modo dev)"
helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null
helm repo add openbao https://openbao.github.io/openbao-helm >/dev/null
helm repo update >/dev/null
helm upgrade --install cnpg cnpg/cloudnative-pg -n cnpg-system --create-namespace \
  ${CNPG_CHART_VERSION:+--version "$CNPG_CHART_VERSION"} --wait --timeout 10m
helm upgrade --install openbao openbao/openbao -n cofre --create-namespace \
  -f "$kind_dir/openbao-valores.yaml" ${OPENBAO_CHART_VERSION:+--version "$OPENBAO_CHART_VERSION"} \
  --wait --timeout 10m
kubectl -n cofre wait --for=condition=Ready pod/openbao-0 --timeout=300s
fim

grupo "SeaweedFS (S3) de um nó"
s3_id="$(aleatorio)"
s3_chave="$(aleatorio)"
kubectl create namespace armazenamento --dry-run=client -o yaml | aplicar
printf '{"identities":[{"name":"rapa","credentials":[{"accessKey":"%s","secretKey":"%s"}],"actions":["Admin","Read","Write","List","Tagging"]}]}' \
  "$s3_id" "$s3_chave" >"$trabalho/s3.json"
kubectl -n armazenamento create secret generic seaweedfs-s3 --from-file=s3.json="$trabalho/s3.json" \
  --dry-run=client -o yaml | aplicar
kubectl apply -f "$kind_dir/seaweedfs.yaml"
kubectl -n armazenamento rollout status deployment seaweedfs --timeout=300s
for _ in $(seq 1 30); do
  if kubectl -n armazenamento exec deploy/seaweedfs -- sh -c \
    'echo "s3.bucket.create -name rapa-artefatos" | weed shell -master=localhost:9333' >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
fim

grupo "Portal de teste"
kubectl apply -f "$kind_dir/portal.yaml"
kubectl -n rapa-teste rollout status deployment portal --timeout=300s
fim

grupo "Segredos do RaPA (só os nomes vão para o chart)"
kubectl create namespace "$ns" --dry-run=client -o yaml | aplicar
for papel in dono:rapa_owner app:rapa_app despachante:rapa_dispatcher; do
  kubectl -n "$ns" create secret generic "rapa-banco-${papel%%:*}" --type=kubernetes.io/basic-auth \
    --from-literal=username="${papel##*:}" --from-literal=password="$(aleatorio)" \
    --dry-run=client -o yaml | aplicar
done
kubectl -n "$ns" create secret generic rapa-s3 --from-literal=AWS_ACCESS_KEY_ID="$s3_id" \
  --from-literal=AWS_SECRET_ACCESS_KEY="$s3_chave" --dry-run=client -o yaml | aplicar
# Tokens provisórios: a empresa só existe depois da migração.
printf '{"token-provisorio": {"sujeito": "ninguem", "empresa": "00000000-0000-0000-0000-000000000000"}}' \
  >"$trabalho/tokens.json"
kubectl -n "$ns" create secret generic rapa-dev-tokens --from-file=tokens.json="$trabalho/tokens.json" \
  --dry-run=client -o yaml | aplicar
fim

# A API do Kubernetes do kind, para as NetworkPolicies (o endereço do nó, depois do DNAT).
api_ip="$(kubectl get endpoints kubernetes -n default -o jsonpath='{.subsets[0].addresses[0].ip}')"
api_porta="$(kubectl get endpoints kubernetes -n default -o jsonpath='{.subsets[0].ports[0].port}')"
rede_api="[{\"ipBlock\":{\"cidr\":\"${api_ip}/32\"}}]"
portas_api="[${api_porta},443]"

grupo "Release do banco (CloudNativePG)"
helm upgrade --install rapa-banco "$chart" -n "$ns" -f "$chart/values-kind.yaml" \
  -f "$chart/values-banco.yaml" --set-json "network.egress.kubernetesApi.to=$rede_api" \
  --set-json "network.egress.kubernetesApi.ports=$portas_api" --wait --timeout 10m
kubectl -n "$ns" wait cluster/rapa-banco --for=condition=Ready --timeout=600s
# Os papéis da aplicação e do despachante (managed.roles) antes da migração, que só concede
# permissões aos papéis que existem.
papeis=""
for _ in $(seq 1 60); do
  papeis="$(kubectl -n "$ns" get cluster rapa-banco \
    -o jsonpath='{.status.managedRolesStatus.byStatus.reconciled}')"
  if [[ "$papeis" == *rapa_app* && "$papeis" == *rapa_dispatcher* ]]; then
    break
  fi
  sleep 5
done
[[ "$papeis" == *rapa_app* && "$papeis" == *rapa_dispatcher* ]]
fim

grupo "Preparo do OpenBao (deploy/openbao/preparar.sh)"
kubectl cp "$raiz/deploy/openbao" cofre/openbao-0:/tmp/openbao
kubectl -n cofre exec openbao-0 -- env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN=root \
  RAPA_NAMESPACE="$ns" KUBERNETES_HOST=https://kubernetes.default.svc \
  sh /tmp/openbao/preparar.sh
fim

grupo "Release da aplicação (a migração roda antes, como hook)"
helm upgrade --install rapa "$chart" -n "$ns" -f "$chart/values-kind.yaml" \
  --set-json "network.egress.kubernetesApi.to=$rede_api" \
  --set-json "network.egress.kubernetesApi.ports=$portas_api" --wait --timeout 15m
fim

grupo "Empresa de teste pelo CLI e os tokens dela"
admin() { kubectl -n "$ns" exec deploy/rapa-api -- python -m rapa.control.admin "$@"; }
empresa="$(admin empresa criar "Empresa de teste" --organizacao org-teste --admin pessoa-admin \
  --nome-admin "Pessoa administradora" --area Fiscal --vagas 2 --por ci | tail -n 1)"
admin usuario "$empresa" pessoa-criadora --nome "Pessoa criadora" --papel creator --por ci
printf '{"%s": {"sujeito": "pessoa-admin", "empresa": "%s"}, "%s": {"sujeito": "pessoa-criadora", "empresa": "%s"}}' \
  "$token_admin" "$empresa" "$token_criadora" "$empresa" >"$trabalho/tokens.json"
kubectl -n "$ns" create secret generic rapa-dev-tokens --from-file=tokens.json="$trabalho/tokens.json" \
  --dry-run=client -o yaml | aplicar
for componente in web api; do
  kubectl -n "$ns" rollout restart "deployment/rapa-$componente"
done
for componente in web api; do
  kubectl -n "$ns" rollout status "deployment/rapa-$componente" --timeout=600s
done
echo "empresa de teste: $empresa"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "empresa=$empresa" >>"$GITHUB_OUTPUT"
fi
fim

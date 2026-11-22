#!/usr/bin/env bash
# Recolhe o estado e os logs do cluster kind quando o job falha (vai como artefato do CI).
# Os logs do RaPA não têm segredos (regra do projeto); os Secrets não são lidos.
set -uo pipefail

saida="${1:-logs-kind}"
mkdir -p "$saida"
kubectl get nodes -o wide >"$saida/nos.txt" 2>&1
kubectl get all,networkpolicies,clusters.postgresql.cnpg.io -A -o wide >"$saida/recursos.txt" 2>&1
kubectl get events -A --sort-by=.lastTimestamp >"$saida/eventos.txt" 2>&1
for ns in rapa rapa-execucoes rapa-teste cofre armazenamento cnpg-system; do
  kubectl -n "$ns" describe pods >"$saida/pods-$ns.txt" 2>&1
  for pod in $(kubectl -n "$ns" get pods -o name 2>/dev/null); do
    nome="${pod#pod/}"
    kubectl -n "$ns" logs "$pod" --all-containers --prefix --tail=2000 \
      >"$saida/log-$ns-$nome.txt" 2>&1
    kubectl -n "$ns" logs "$pod" --all-containers --prefix --previous --tail=2000 \
      >"$saida/log-$ns-$nome-anterior.txt" 2>/dev/null || rm -f "$saida/log-$ns-$nome-anterior.txt"
  done
done
kubectl -n rapa get jobs -o yaml >"$saida/jobs-rapa.yaml" 2>&1
echo "logs em $saida"

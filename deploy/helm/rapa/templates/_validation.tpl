{{/*
Regras que o chart confere antes de renderizar qualquer coisa: uma configuração perigosa
falha aqui, com a mensagem do que corrigir, em vez de subir aberta.
*/}}

{{/*
Redes que os robôs nunca alcançam, nem pela lista de redes internas permitidas nem pelo
proxy: o "este host" e o loopback, o link-local (o metadata da nuvem em 169.254.169.254), o
multicast e o reservado; em IPv6, o não especificado, o loopback e o compatível com IPv4
(::/96), o IPv4 mapeado, o NAT64 (use a rede de IPv4), o 6to4, o metadata, o DNS e o NTP da
AWS (fd00:ec2::/32), o link-local e o multicast.
*/}}
{{- define "rapa.neverOpen" -}}
- 0.0.0.0/8
- 127.0.0.0/8
- 169.254.0.0/16
- 224.0.0.0/3
- ::/96
- ::ffff:0:0/96
- 64:ff9b::/96
- 64:ff9b:1::/48
- 2002::/16
- fd00:ec2::/32
- fe80::/10
- ff00::/8
{{- end -}}

{{/* As redes privadas de IPv4 em que ficam os pods (o FORWARDED_ALLOW_IPS só confia nelas). */}}
{{- define "rapa.podNetworksIPv4" -}}
- 10.0.0.0/8
- 172.16.0.0/12
- 192.168.0.0/16
- 100.64.0.0/10
{{- end -}}

{{- define "rapa.validate" -}}
{{- $v := .Values -}}
{{- $dev := $v.auth.dev.enabled -}}

{{- if not (has $v.vault.mode (list "nenhum" "dev" "openbao")) -}}
{{- fail "vault.mode precisa ser nenhum, dev ou openbao (RAPA_COFRE)" -}}
{{- end -}}
{{- if not (has $v.storage.mode (list "local" "s3")) -}}
{{- fail "storage.mode precisa ser local ou s3 (RAPA_ARMAZENAMENTO)" -}}
{{- end -}}
{{- if not (has $v.keys.mode (list "local" "openbao")) -}}
{{- fail "keys.mode precisa ser local ou openbao (RAPA_CHAVES)" -}}
{{- end -}}

{{/*
As NetworkPolicies fecham as redes privadas do cluster (o banco, o OpenBao, o S3, a API do
Kubernetes) para os robôs: a guarda de saída deixa essas redes para a política. Desligadas,
só em kind e desenvolvimento.
*/}}
{{- if and (not $v.network.policies.enabled) (not $dev) -}}
{{- fail "network.policies.enabled=false só vale com auth.dev.enabled, em kind e desenvolvimento: sem as NetworkPolicies, os robôs alcançam o banco, o OpenBao, o S3 e a API do Kubernetes" -}}
{{- end -}}

{{/* Nenhuma URL com usuário e senha: o valor vai em claro para o manifesto e para os pods. */}}
{{- $urls := dict
  "publicUrl" $v.publicUrl
  "auth.jwt.issuer" $v.auth.jwt.issuer
  "auth.jwt.jwksUrl" $v.auth.jwt.jwksUrl
  "auth.oidc.redirectUri" $v.auth.oidc.redirectUri
  "vault.address" $v.vault.address
  "storage.s3.endpoint" $v.storage.s3.endpoint
  "observability.otlpEndpoint" $v.observability.otlpEndpoint
  "modelHub.baseUrl" $v.modelHub.baseUrl
  "executions.egress.proxy.url" $v.executions.egress.proxy.url
  "database.cnpg.backup.endpointURL" $v.database.cnpg.backup.endpointURL
-}}
{{- range $name := keys $urls | sortAlpha -}}
{{- if regexMatch "^[A-Za-z][A-Za-z0-9+.-]*://[^/?#]*@" (toString (index $urls $name)) -}}
{{- fail (printf "%s não pode levar usuário e senha na URL (o valor vai em claro para o manifesto); para o proxy de saída, use executions.egress.proxy.existingSecret" $name) -}}
{{- end -}}
{{- end -}}

{{/*
Fora do desenvolvimento, os destinos que recebem um token ou um segredo só por https: o
OpenBao (o token e as chaves), o S3 (as credenciais e os artefatos), o Keycloak (as chaves
dos tokens), o hub do modelo (a chave dele), o endereço público e o S3 dos backups do banco.
*/}}
{{- if not $dev -}}
{{- $usesBao := or (eq $v.keys.mode "openbao") (eq $v.vault.mode "openbao") -}}
{{- $https := list
  (list "publicUrl" $v.publicUrl true)
  (list "auth.jwt.issuer" $v.auth.jwt.issuer true)
  (list "auth.jwt.jwksUrl" $v.auth.jwt.jwksUrl true)
  (list "vault.address" $v.vault.address $usesBao)
  (list "storage.s3.endpoint" $v.storage.s3.endpoint (eq $v.storage.mode "s3"))
  (list "modelHub.baseUrl" $v.modelHub.baseUrl true)
  (list "database.cnpg.backup.endpointURL" $v.database.cnpg.backup.endpointURL (and $v.database.cnpg.enabled $v.database.cnpg.backup.enabled))
-}}
{{- range $item := $https -}}
{{- $url := toString (index $item 1) -}}
{{- if and (index $item 2) $url (not (hasPrefix "https://" $url)) -}}
{{- fail (printf "%s precisa ser https:// fora do desenvolvimento (auth.dev.enabled): %q" (index $item 0) $url) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* A restauração do banco (database.cnpg.recovery): um Cluster novo, nunca o de origem. */}}
{{- $recovery := $v.database.cnpg.recovery -}}
{{- if and $v.database.cnpg.enabled $recovery.enabled -}}
{{- if or (not $recovery.source) (eq $recovery.source $v.database.cnpg.name) -}}
{{- fail "database.cnpg.recovery: o Cluster restaurado precisa de outro nome (database.cnpg.name) e da origem em database.cnpg.recovery.source: o WAL dos dois não se mistura" -}}
{{- end -}}
{{- if not $v.database.cnpg.backup.destinationPath -}}
{{- fail "database.cnpg.recovery precisa de database.cnpg.backup (o caminho dos backups da origem)" -}}
{{- end -}}
{{- end -}}

{{- if $v.app.enabled -}}

{{/* O Job de migração roda antes dos recursos da release: o banco não pode estar nela. */}}
{{- if and $v.database.cnpg.enabled $v.migration.enabled -}}
{{- fail "database.cnpg.enabled precisa de uma release própria do banco (app.enabled=false), instalada antes: o Job de migração roda antes de qualquer recurso da release da aplicação" -}}
{{- end -}}

{{/* Os artefatos do disco de um pod não chegam aos outros: no cluster, só o S3. */}}
{{- if ne $v.storage.mode "s3" -}}
{{- fail "storage.mode=local não serve no cluster (a web, a api e o despachante são pods diferentes): use storage.mode=s3" -}}
{{- end -}}

{{/* O modo de desenvolvimento (tokens fixos, login sem senha) nunca atrás do gateway público. */}}
{{- if and $dev $v.gateway.enabled -}}
{{- fail "auth.dev.enabled (RAPA_DEV) não pode ir junto com gateway.enabled: tokens fixos e login sem senha nunca ficam expostos" -}}
{{- end -}}
{{- if and $dev (not $v.auth.dev.tokensSecret) -}}
{{- fail "auth.dev.enabled precisa de auth.dev.tokensSecret (RAPA_TOKENS_DEV)" -}}
{{- end -}}
{{- if and (eq $v.vault.mode "dev") (not (and $dev $v.auth.dev.secretsSecret)) -}}
{{- fail "vault.mode=dev precisa de auth.dev.enabled e auth.dev.secretsSecret (RAPA_SEGREDOS_DEV)" -}}
{{- end -}}
{{- if and $v.gateway.enabled (not $v.auth.jwt.issuer) -}}
{{- fail "o gateway público precisa de auth.jwt (RAPA_JWT_ISSUER, RAPA_JWT_AUDIENCE e RAPA_JWKS_URL)" -}}
{{- end -}}
{{- if and $v.auth.jwt.issuer (not (and $v.auth.jwt.audience $v.auth.jwt.jwksUrl)) -}}
{{- fail "auth.jwt precisa de issuer, audience e jwksUrl juntos" -}}
{{- end -}}

{{/* O que as contas da web e do despachante criam no namespace das execuções (admission.yaml). */}}
{{- if and (not $v.executions.admissionPolicy.enabled) (not $dev) -}}
{{- fail "executions.admissionPolicy.enabled=false só vale com auth.dev.enabled: sem a política, o token da web ou do despachante cria qualquer pod no namespace das execuções" -}}
{{- end -}}

{{/* Sem gVisor, só em kind e desenvolvimento. */}}
{{- if and (not $v.executions.runtimeClass) (not $dev) -}}
{{- fail "executions.runtimeClass vazio (sem gVisor) só vale com auth.dev.enabled, em kind e desenvolvimento" -}}
{{- end -}}

{{/*
O banco sempre com TLS conferido fora do desenvolvimento: verify-full (ou verify-ca) com o CA
do CloudNativePG. require cifra mas não confere quem responde.
*/}}
{{- if and (not $dev) (not (has $v.database.sslMode (list "verify-full" "verify-ca"))) -}}
{{- fail "database.sslMode precisa ser verify-full ou verify-ca (com database.caSecret.name) fora do desenvolvimento: require não confere o servidor" -}}
{{- end -}}
{{- if and (has $v.database.sslMode (list "verify-full" "verify-ca")) (not $v.database.caSecret.name) -}}
{{- fail "database.sslMode verify-full ou verify-ca precisa de database.caSecret.name" -}}
{{- end -}}

{{/*
FORWARDED_ALLOW_IPS: atrás do gateway, as redes dos pods do Envoy, e só elas. Sem elas, o
uvicorn vê o Envoy como origem de todos os pedidos (um limite por endereço vira um limite
para todos, e o X-Forwarded-Proto se perde); com uma rede larga, qualquer um forja a origem.
*/}}
{{- if and $v.gateway.enabled (not $v.gateway.trustedNetworks) -}}
{{- fail "gateway.enabled precisa de gateway.trustedNetworks (FORWARDED_ALLOW_IPS): as redes dos pods do Envoy" -}}
{{- end -}}
{{- $neverOpen := fromYamlArray (include "rapa.neverOpen" .) -}}
{{- range $v.gateway.trustedNetworks -}}
{{- include "rapa.cidrCheck" (dict "text" . "what" "gateway.trustedNetworks (FORWARDED_ALLOW_IPS, nunca * nem a internet)" "minV4" 8 "minV6" 48 "forbidden" $neverOpen "within" (fromYamlArray (include "rapa.podNetworksIPv4" $))) -}}
{{- end -}}

{{/*
Redes internas permitidas aos robôs (RAPA_REDES_INTERNAS_PERMITIDAS) e as do proxy de saída:
CIDRs estreitos (/16 ou mais em IPv4, /48 ou mais em IPv6) e longe das redes que nunca se
abrem. As redes internas a mais (que só fecham) precisam ser CIDRs, sem a internet inteira.
*/}}
{{- $e := $v.executions.egress -}}
{{- range $e.allowedInternalNetworks -}}
{{- include "rapa.cidrCheck" (dict "text" . "what" "executions.egress.allowedInternalNetworks (RAPA_REDES_INTERNAS_PERMITIDAS)" "minV4" 16 "minV6" 48 "forbidden" $neverOpen) -}}
{{- end -}}
{{- range $e.proxy.cidrs -}}
{{- include "rapa.cidrCheck" (dict "text" . "what" "executions.egress.proxy.cidrs" "minV4" 16 "minV6" 48 "forbidden" $neverOpen) -}}
{{- end -}}
{{- range $e.extraInternalNetworks -}}
{{- include "rapa.cidrCheck" (dict "text" . "what" "executions.egress.extraInternalNetworks" "minV4" 1 "minV6" 1 "forbidden" (list)) -}}
{{- end -}}

{{/* O proxy de saída (RAPA_PROXY_SAIDA): pela URL ou, com senha, por um Secret. */}}
{{- if and $e.proxy.url $e.proxy.existingSecret -}}
{{- fail "executions.egress.proxy: informe url ou existingSecret, não os dois" -}}
{{- end -}}
{{- $proxy := or $e.proxy.url $e.proxy.existingSecret -}}
{{- if and $proxy (not $e.proxy.cidrs) -}}
{{- fail "executions.egress.proxy.url precisa de executions.egress.proxy.cidrs (a NetworkPolicy só conhece endereços)" -}}
{{- end -}}
{{- if and (not $e.directInternet) (not $proxy) -}}
{{- fail "sem executions.egress.directInternet, os robôs só saem pelo proxy: informe executions.egress.proxy.url" -}}
{{- end -}}

{{/*
Num cluster IPv6, os endereços dos pods e dos serviços são globais (fora de fc00::/7): sem o
bloco da VPC em extraInternalNetworks, a regra da internet (::/0) abriria o cluster.
*/}}
{{- $ipv6 := or $v.executions.channel.ipv6 (contains ":" (toString $v.network.listenAddress)) -}}
{{- if $ipv6 -}}
{{- $blocked := false -}}
{{- range $e.extraInternalNetworks }}{{ if contains ":" (toString .) }}{{ $blocked = true }}{{ end }}{{ end -}}
{{- if not $blocked -}}
{{- fail "um cluster IPv6 (executions.channel.ipv6 ou network.listenAddress com \":\") precisa do bloco IPv6 da VPC (o dos pods e dos serviços) em executions.egress.extraInternalNetworks" -}}
{{- end -}}
{{- end -}}

{{/*
Regras de saída a mais para os robôs: só em kind e desenvolvimento (o portal de teste), e só
para pods escolhidos por namespace (nunca um ipBlock, que passaria por cima das redes
internas e da guarda), sempre com as portas.
*/}}
{{- with $e.extraRules -}}
{{- if not $dev -}}
{{- fail "executions.egress.extraRules só vale com auth.dev.enabled, em kind e desenvolvimento (o portal de teste): em produção, use allowedInternalNetworks, que também chega à guarda de saída" -}}
{{- end -}}
{{- range $rule := . -}}
{{- if not (and (kindIs "map" $rule) $rule.to $rule.ports) -}}
{{- fail "cada regra de executions.egress.extraRules precisa de to e ports" -}}
{{- end -}}
{{- range $peer := $rule.to -}}
{{- if not (and (kindIs "map" $peer) $peer.namespaceSelector (not (hasKey $peer "ipBlock"))) -}}
{{- fail "executions.egress.extraRules: cada destino precisa de um namespaceSelector com rótulos, e nunca de um ipBlock" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
As portas do despachante não se cruzam (o processo recusa na partida): a da saúde, a do canal
e a das métricas. A do host de sessão é fixa no código (o relay conecta nela).
*/}}
{{- $health := int $v.dispatcher.healthPort -}}
{{- $channelPort := int $v.executions.channel.port -}}
{{- $metricsPort := int $v.observability.metricsPort -}}
{{- if eq $health $channelPort -}}
{{- fail "dispatcher.healthPort precisa ser outra porta que executions.channel.port: o canal dos pods tem o servidor dele" -}}
{{- end -}}
{{- if and (ne $metricsPort 0) (or (eq $metricsPort $health) (eq $metricsPort $channelPort) (eq $metricsPort 8000)) -}}
{{- fail "observability.metricsPort precisa ser outra porta que a do serviço (8000 ou dispatcher.healthPort) e a do canal" -}}
{{- end -}}
{{- if ne (int $v.executions.sessionPort) 8765 -}}
{{- fail "executions.sessionPort é a porta fixa do host de sessão (8765): o chart não a troca" -}}
{{- end -}}

{{/* Em produção, as imagens pelo digest. */}}
{{- if $v.images.requireDigest -}}
{{- if not (and $v.images.control.digest $v.images.executor.digest) -}}
{{- fail "images.requireDigest: informe images.control.digest e images.executor.digest (sha256:...)" -}}
{{- end -}}
{{- end -}}
{{- range list $v.images.control.digest $v.images.executor.digest -}}
{{- if and . (not (regexMatch "^sha256:[0-9a-f]{64}$" .)) -}}
{{- fail (printf "digest inválido: %q (use sha256:<64 hexadecimais>)" .) -}}
{{- end -}}
{{- end -}}

{{- end -}}
{{- end -}}

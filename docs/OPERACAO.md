# Operação do RaPA

Roteiro do time da plataforma em duas partes: o RaPA no Kubernetes (EKS no AWS Outposts: instalar, atualizar, o que cada valor do chart e cada variável do serviço faz, backup e restauração do banco, o cofre, as chaves, os incidentes, a observabilidade e a saúde) e a operação de cada empresa (entrada, consumo, congelar, exportar e apagar).

## Parte 1. O RaPA no Kubernetes

A única peça da AWS é o EKS no Outposts, com nós autogerenciados no rack. Todo o resto é código aberto e roda no cluster, instalado pelo próprio chart de cada componente e só configurado por nós: o operador CloudNativePG (com o plugin Barman Cloud, que pede o cert-manager), o OpenBao, o Keycloak, o SeaweedFS (S3), o Envoy Gateway, o VictoriaMetrics, o GlitchTip, o Argo CD e o gVisor nos nós de execução.

### 1.1 O que roda onde

O chart `deploy/helm/rapa` instala, no namespace `rapa` (o plano de controle):

- `rapa-web`: a interface, a REST, o MCP e as sessões de gravação. Uma réplica por padrão: a interface e as sessões ficam na memória do processo. Com mais de uma, o chart põe a afinidade por cookie no Envoy (`BackendTrafficPolicy`), e cada navegador fica sempre na mesma réplica; um cliente da REST que não guarda cookies pode cair noutra réplica e não ver a sessão que abriu.
- `rapa-api`: só a REST e o MCP (`RAPA_UI=0`, `RAPA_SESSOES=0`), duas réplicas, HPA opcional.
- `rapa-despachante`: a fila e os pods de uso único (`RAPA_HTTP=0`, `RAPA_EXECUTOR=kubernetes`), duas réplicas. O processo ouve duas portas: a da saúde (`/saude` e `/saude/pronto`, só para os probes; `dispatcher.healthPort`, 8080, o `--port` do serviço) e a do canal dos pods de execução (`RAPA_CANAL_PORTA`, 8081, um servidor próprio, com os limites do canal); o processo recusa subir com as duas iguais. Cada réplica guarda na memória os bilhetes das tentativas que lançou, então o endereço do canal que vai para cada pod é `RAPA_CANAL_ENDERECO=http://{ip}:8081`, com `{ip}` trocado pelo IP da própria réplica, nunca um Service que espalhe entre elas (um endereço sem `{ip}` é recusado na partida).
- O Job de migração (hook `pre-install,pre-upgrade`; no Argo CD, PreSync) e, opcional, o de permissões (`--somente-permissoes`, depois de instalar).
- O modelo do pod da operação (o `PodTemplate` `rapa-operacao`, com a conta de serviço e o papel do OpenBao da operação): nada roda sozinho; quem opera cria um pod dele para congelar, exportar e apagar uma empresa (1.10).
- As NetworkPolicies, o RBAC, as contas de serviço, a `ValidatingAdmissionPolicy` dos pods de uso único e as `HTTPRoute`.

No namespace `rapa-execucoes` (criado pelo chart com a Pod Security `restricted`) rodam os pods de uso único de cada execução (`app.kubernetes.io/name=rapa-executor`) e de cada sessão de gravação (`rapa-sessao`), com a imagem do executor e a RuntimeClass `gvisor`, sem token de conta de serviço e sem permissão nenhuma. O despachante e a web têm ali uma Role para criar, ler e apagar pods e Secrets, e o que elas criam passa pela `ValidatingAdmissionPolicy` `rapa-execucoes-pods-de-uso-unico`: só a imagem do executor (pelo digest), a conta `rapa-executor` sem token, a RuntimeClass `gvisor`, o rótulo de cada conta (o despachante só cria pods de execução, e a web, só de sessão), o comando exato de execução ou de sessão, sem ganchos (`lifecycle`) nem sondas além da prontidão `tcpSocket` da sessão, só `PYTHONUNBUFFERED` e `TMPDIR` no ambiente e volumes `secret` e `emptyDir` montados só nas pastas do lançador (o Secret na dele, os `emptyDir` em `/tmp` e `/trabalho`, sem `subPath`). Com o token de uma dessas contas, ninguém cria ali um pod no runc, com outra imagem ou que rode outro código, e o da web não cria o pod com o rótulo que abre o canal. A Role lê os Secrets do namespace (o lançador lista os das tentativas): o `rapa-registro` dali só com permissão de baixar imagens.

O banco (CloudNativePG) vem de outra release do mesmo chart (`rapa-banco`, com `values-banco.yaml`), instalada antes: o Job de migração roda antes de qualquer recurso da release da aplicação, então o banco não pode estar nela (o chart recusa).

Rede (NetworkPolicies, os dois namespaces negam tudo por padrão):

- `rapa-web` e `rapa-api` só recebem do Envoy (`gateway.pods`) e do coletor de métricas, na porta de métricas.
- `rapa-despachante` só recebe dos pods de execução, na porta do canal (e do coletor de métricas, na porta de métricas); os probes vêm do kubelet, no próprio nó, que os CNIs com NetworkPolicy deixam passar (como os da web e da api).
- O plano de controle só sai para o DNS, o banco, o OpenBao, o Keycloak, o S3, o hub do modelo, a API do Kubernetes e a observabilidade (`network.egress`), cada componente só para o que usa: a api nunca fala com a API do Kubernetes; o despachante nunca fala com o Keycloak nem com o hub; o pod da operação só fala com o banco, o S3 e o OpenBao. Um destino sem endereços fica fechado. O banco é sempre o Cluster de `database.cnpg.name` e o Pooler dele (o nome vale nas duas releases).
- Os pods de execução e de sessão só saem para o DNS, para o canal do despachante (só os de execução) e para a internet menos as redes internas (10/8, 172.16/12, 192.168/16, 100.64/10, 169.254/16, 127/8, 0/8 e as de IPv6 equivalentes, inclusive as que o NAT64 e o 6to4 carregam), mais as de `executions.egress.allowedInternalNetworks` (`RAPA_REDES_INTERNAS_PERMITIDAS`) e o proxy de saída (`RAPA_PROXY_SAIDA`). A web conecta no WebSocket dos pods de sessão (o relay da visão ao vivo). Uma rede interna fora dessas (o pool de IPs do cliente no Outposts, uma rede da empresa fora da RFC 1918) vai em `executions.egress.extraInternalNetworks`; as de IPv4 ali fecham também pelo NAT64 (`64:ff9b::<v4>`).
- Num cluster IPv6 (`network.listenAddress: "::"` ou `executions.channel.ipv6`), os endereços dos pods e dos serviços são globais, fora de `fc00::/7`: o bloco IPv6 da VPC em `executions.egress.extraInternalNetworks` é obrigatório (o chart recusa sem ele), senão a regra da internet (`::/0`) abriria o OpenBao, o Keycloak e o S3.

### 1.2 Ordem de instalação

1. **Nós.** Dois grupos de nós autogerenciados no Outposts: o do plano de controle e o das execuções, com o gVisor (`runsc` como handler do containerd), o rótulo `rapa.dev/execucoes=sim` e o taint `rapa.dev/execucoes:NoSchedule`. Aplique `deploy/gvisor/runtimeclass.yaml` (a RuntimeClass `gvisor` já leva os pods para esses nós).

   **O CNI precisa aplicar as NetworkPolicies.** Todo o isolamento de 1.1 vem delas (os pods de execução e de sessão longe do metadata, da API do Kubernetes, do banco, do despachante e dos outros pods; o plano de controle só para os destinos dele), e um CNI que não as aplica as aceita e ignora, calado. No EKS, o Amazon VPC CNI só as aplica com o complemento configurado com `enableNetworkPolicy: "true"` (o agente de políticas em cada nó, os do Outposts também; confira que a versão do complemento e dos nós o suporta); a outra saída é um CNI que as aplique (Calico, Cilium). Sem isso, não instale o chart: o teste de saída (1.9) mostra o problema.
2. **Componentes externos**, cada um pelo chart dele: cert-manager, o operador CloudNativePG e o plugin Barman Cloud (`cnpg-system`), o OpenBao em HA com TLS (`cofre`; veja 1.6 para iniciar e desbloquear), o Keycloak (`identidade`; `deploy/keycloak/README.md`), o SeaweedFS com o S3 em TLS e os buckets `rapa-artefatos` e `rapa-backups` (`armazenamento`), o Envoy Gateway com o `Gateway` externo `rapa-externo`, o certificado TLS dele e um listener HTTPS que aceita rotas do namespace `rapa` (`allowedRoutes.namespaces`) (`envoy-gateway-system`; as redes dos pods do Envoy vão para `gateway.trustedNetworks`), o VictoriaMetrics (`monitoramento`), o GlitchTip e o Argo CD (`argocd`).
3. **OpenBao**: `deploy/openbao/preparar.sh` monta o KV, o transit, o método kubernetes e os quatro papéis, um por conta de serviço e só para ela: `rapa-despachante` (lê os segredos e embrulha as chaves), `rapa-web` e `rapa-api` (só desembrulham) e `rapa-operacao` (o pod da operação: apaga os segredos e a chave de uma empresa). São os valores `vault.roles` do chart (`RAPA_BAO_PAPEL` de cada componente); a tabela está em `deploy/openbao/README.md`. O script roda de qualquer máquina com o CLI `bao` e o token de quem administra o cofre (no kind, do runner do CI, pelo port-forward); com o OpenBao fora de um pod do cluster, informe o CA da API em `KUBERNETES_CA_CERT` e, obrigatório junto, `KUBERNETES_TOKEN_REVIEWER_JWT` (o arquivo com o token de uma conta de serviço com `system:auth-delegator`, que o OpenBao usa na TokenReview; informe-o a cada execução, porque o script reescreve o config inteiro).
4. **Segredos.** Crie os Secrets no namespace `rapa` antes do chart (o chart só leva os nomes; nenhum valor fica no Git nem nos valores). As senhas, sempre aleatórias e só com caracteres seguros numa URL (`openssl rand -hex 32`), de um arquivo 0600 e nunca na linha de comando:

   | Secret | Chaves | Para |
   |---|---|---|
   | `rapa-banco-dono`, `rapa-banco-app`, `rapa-banco-despachante` | `username`, `password` (tipo `kubernetes.io/basic-auth`) | os papéis `rapa_owner`, `rapa_app` e `rapa_dispatcher` (o CloudNativePG cria os papéis com essas senhas) |
   | `rapa-banco-backup-s3` | `ACCESS_KEY_ID`, `ACCESS_SECRET_KEY` | o bucket dos backups |
   | `rapa-s3` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | o bucket dos artefatos |
   | `rapa-openbao-ca`, `rapa-s3-ca` | `ca.crt` | os CAs do OpenBao e do S3 |
   | `rapa-oidc` | `client-secret` | o cliente `rapa-interface` do Keycloak |
   | `rapa-webhooks` | `chave` | `RAPA_CHAVE_WEBHOOKS` (32 caracteres ou mais) |
   | `rapa-hub` | `api-key` | `RAPA_LLM_API_KEY` |
   | `rapa-glitchtip` | `dsn` | `RAPA_GLITCHTIP_DSN` |
| `RAPA_DOCS_API` | `0` | `1` serve a documentação da API REST em `/v1/docs` e `/v1/openapi.json` (1.13); no chart, `apiDocs` |
   | `rapa-registro` | `.dockerconfigjson` | o registro das imagens, nos dois namespaces (só com permissão de baixar: a Role da web e do despachante lê os Secrets do namespace das execuções) |
   | `rapa-proxy` (opcional) | `url` | `RAPA_PROXY_SAIDA` com usuário e senha (`http://usuario:senha@host:porta`, os caracteres reservados com `%`; `executions.egress.proxy.existingSecret`); a URL nunca vai nos valores |

   ```bash
   umask 077
   openssl rand -hex 32 | tr -d '\n' > /tmp/senha    # sem a quebra de linha no fim
   kubectl -n rapa create secret generic rapa-banco-app --type=kubernetes.io/basic-auth \
     --from-literal=username=rapa_app --from-file=password=/tmp/senha
   shred -u /tmp/senha
   ```

   Com um registro privado, crie o namespace `rapa-execucoes` antes (com os rótulos `pod-security.kubernetes.io/enforce=restricted`), o `rapa-registro` nele também, e use `executions.createNamespace: false`.
5. **Banco**: a release `rapa-banco` e a espera pelo Cluster pronto.

   ```bash
   helm upgrade --install rapa-banco deploy/helm/rapa -n rapa -f valores-do-ambiente.yaml -f deploy/helm/rapa/values-banco.yaml
   kubectl -n rapa wait cluster/rapa-banco --for=condition=Ready --timeout=20m
   kubectl -n rapa get cluster rapa-banco -o jsonpath='{.status.managedRolesStatus.byStatus.reconciled}'
   ```

   Espere `rapa_app` e `rapa_dispatcher` aparecerem entre os papéis reconciliados: a migração só concede permissões aos papéis que existem (se eles chegarem depois, o Job de permissões, `migration.grantsJob.enabled`, concede de novo depois de instalar).

6. **Aplicação**: a release `rapa` (a migração roda antes, como hook).

   ```bash
   helm upgrade --install rapa deploy/helm/rapa -n rapa -f valores-do-ambiente.yaml
   ```

   Com o Argo CD, as duas releases são as duas Applications de `deploy/argocd/application.yaml`, filhas da Application raiz `deploy/argocd/raiz.yaml`. As ondas (`sync-wave`) só ordenam o que está dentro do sync de uma Application: é a raiz que cria a do banco (onda -1, com o prune desligado), espera ela ficar saudável (o Cluster pronto) e só então cria a da aplicação (onda 0), cujo Job de migração (PreSync) já encontra o banco. Para a raiz esperar, o `argocd-cm` precisa das checagens de saúde de `deploy/argocd/argocd-cm-saude.yaml` (a de `argoproj.io/Application`, que o Argo CD não traz desde a 1.8, e a do Cluster do CloudNativePG):

   ```bash
   kubectl -n argocd patch configmap argocd-cm --type merge --patch-file deploy/argocd/argocd-cm-saude.yaml
   kubectl apply -f deploy/argocd/raiz.yaml
   ```

   Aplicadas soltas, sem a raiz, as duas Applications sincronizam ao mesmo tempo e a migração só conta com o retry da Application (5 tentativas, até cerca de 15 minutos). Depois da primeira instalação, cada filha sincroniza sozinha: numa troca do banco (1.5), pare o sync automático da raiz e das filhas até o Cluster novo ficar pronto (a raiz primeiro: ela desfaz a mudança nas filhas).
7. **Primeira empresa**: a organização no Keycloak e o CLI (Parte 2), e o `deploy/rede/teste-de-saida.yaml` (1.9).

Os valores de produção partem de `deploy/helm/rapa/values-producao-exemplo.yaml`. O chart recusa, antes de criar qualquer recurso:

- o modo de desenvolvimento junto com o gateway público;
- fora do desenvolvimento: as NetworkPolicies desligadas (`network.policies.enabled`), a política de admissão desligada (`executions.admissionPolicy.enabled`), regras de saída a mais para os robôs (`executions.egress.extraRules`, só para o portal de teste do kind), a RuntimeClass vazia, o banco sem TLS conferido (só `verify-full` ou `verify-ca`, com o CA: `require` cifra sem conferir o servidor) e o OpenBao, o S3, o Keycloak (`auth.jwt`), o hub do modelo, o `publicUrl` e o S3 dos backups do banco sem `https://`;
- o gateway sem `gateway.trustedNetworks` (`FORWARDED_ALLOW_IPS`: sem as redes do Envoy, o uvicorn vê o Envoy como origem de todo pedido, e o limite por endereço vira um limite de todos), e uma rede ali que não fica dentro das redes privadas dos pods (10/8, 172.16/12, 192.168/16, 100.64/10) ou, em IPv6, mais larga que /48 — nunca `*` nem a internet;
- uma rede interna permitida ou do proxy que não é um CIDR canônico (sem zeros à esquerda, sem bits de host: `0.0.0.0/00` é a internet inteira para o Python e para o Kubernetes), mais larga que /16 (IPv4) ou /48 (IPv6), ou que toca o loopback, o link-local e o metadata da nuvem (169.254.169.254, `fd00:ec2::254`, também pelo NAT64 e pelo IPv4 mapeado), o 6to4 ou o multicast;
- um cluster IPv6 sem o bloco da VPC em `executions.egress.extraInternalNetworks`;
- uma URL dos valores com usuário e senha (o proxy com senha vem de um Secret);
- `storage.mode=local` (os artefatos do disco de um pod não chegam aos outros), o proxy de saída sem as redes dele, o banco na release da aplicação, a restauração no mesmo nome do Cluster de origem e, com `images.requireDigest`, as imagens sem o digest.

### 1.3 Atualização

1. Construa e publique as imagens novas e troque os digests (`images.control.digest` e `images.executor.digest`) nos valores do ambiente. As duas imagens são sempre da mesma versão. Ponha o digest do executor que está rodando em `images.executor.previousDigests`: a política de admissão passa a valer na hora da sincronização, e o despachante e a web antigos ainda lançam pods com o executor anterior até saírem (sem isso, as execuções que eles pegam na troca falham com `executor_nao_iniciou`, e as sessões não abrem). Quando a troca terminar (nenhum pod antigo de `rapa-despachante` nem de `rapa-web`), tire-o e sincronize de novo.
2. Sincronize (o Argo CD) ou rode o `helm upgrade`. A migração roda antes de qualquer pod novo; um Job que falha segura a atualização e fica para conferir (`kubectl -n rapa logs job/rapa-migracao`).
3. Os Deployments sobem um pod novo antes de tirar um velho. O despachante que sai espera as execuções em andamento (até 40 minutos: `dispatcher.terminationGracePeriodSeconds`); até ser parado (o SIGTERM), o pod velho ainda pega execuções e as lança com o executor anterior (o de `images.executor.previousDigests`). Os pods de execução já lançados terminam com a imagem com que nasceram.
4. As migrações valem para as duas versões durante a troca (só acrescentam). Voltar a versão do chart não volta o banco: se uma migração precisar ser desfeita, restaure (1.5).

O operador do CloudNativePG, o plugin e os outros componentes têm atualizações próprias, uma de cada vez, com o banco conferido depois de cada uma.

### 1.4 O que cada valor faz

Os nomes do serviço que cada valor preenche (o resto está nos comentários de `values.yaml`):

| Valor | Variável | O que faz |
|---|---|---|
| `images.control`, `images.executor` | `RAPA_K8S_IMAGEM_EXECUTOR` | as imagens (o digest vale no lugar da tag); a do executor é também a dos pods de sessão |
| `images.pullSecrets` | `RAPA_K8S_PULL_SECRET` | o registro (o primeiro também nos pods de uso único) |
| `publicUrl` | `RAPA_PUBLIC_URL` | o endereço público (OpenAPI dos robôs, metadados do MCP, origem da visão ao vivo) |
| `auth.jwt.*` | `RAPA_JWT_ISSUER`, `RAPA_JWT_AUDIENCE`, `RAPA_JWKS_URL` | os tokens do Keycloak |
| `auth.oidc.*` | `RAPA_OIDC_CLIENT_ID`, `RAPA_OIDC_REDIRECT_URI`, `RAPA_OIDC_CLIENT_SECRET` | o login da interface (só na web) |
| `auth.organizationAliases`, `auth.mcpOrigins`, `auth.liveOrigins`, `auth.rateLimitPerSecond`, `auth.cookieSecure` | `RAPA_ORGANIZATION_ALIASES`, `RAPA_MCP_ORIGENS`, `RAPA_ORIGENS_AO_VIVO`, `RAPA_LIMITE_POR_SEGUNDO`, `RAPA_COOKIE_SEGURO` | como no serviço (`rapa.server.ServerSettings`) |
| `auth.dev.*` | `RAPA_DEV`, `RAPA_TOKENS_DEV`, `RAPA_SEGREDOS_DEV` | só no kind e em testes |
| `database.*` | `RAPA_DATABASE_URL`, `RAPA_DISPATCHER_DATABASE_URL` | o banco: o host (o `-rw` ou o Pooler), o TLS conferido com o CA do CloudNativePG, e a senha de cada papel, que entra na URL pela expansão `$(VAR)` do Kubernetes (nunca no manifesto) |
| `database.cnpg.*` | | o Cluster, os papéis, o Pooler, os backups e o monitor (release do banco); `database.cnpg.name` também diz às NetworkPolicies da aplicação qual é o banco; `database.cnpg.recovery` restaura de um backup (1.5) |
| `executions.namespace` | `RAPA_K8S_NAMESPACE_EXECUCOES` | o namespace dos pods de uso único |
| `executions.runtimeClass` | `RAPA_K8S_RUNTIME_CLASS` | `gvisor` (vazio só no kind) |
| `executions.cpu`, `executions.memory` | `RAPA_K8S_CPU`, `RAPA_K8S_MEMORIA` | os limites de cada pod de uso único |
| `executions.serviceAccount` | `RAPA_K8S_SA_EXECUTOR` | a conta deles, sem permissão e sem token |
| `executions.nodeSelector`, `executions.tolerations` | `RAPA_K8S_NODE_SELECTOR`, `RAPA_K8S_TOLERATIONS` | onde eles rodam (JSON) |
| `executions.channel.port`, `executions.channel.ipv6` | `RAPA_CANAL_PORTA`, `RAPA_CANAL_ENDERECO` | o canal do despachante (o endereço é `http://{ip}:<porta>`: o RaPA troca `{ip}` pelo IP de cada réplica, com colchetes em IPv6); `ipv6` marca o cluster IPv6 (pede o bloco da VPC) |
| `executions.sessionPort` | | a porta do WebSocket do host de sessão (8765, fixa no código: o chart recusa outra) |
| `dispatcher.healthPort` | `--port` do despachante | a saúde do despachante para os probes (8080; nunca a do canal nem a das métricas) |
| `executions.egress.proxy` | `RAPA_PROXY_SAIDA` | o proxy CONNECT corporativo (a URL, ou com senha, `existingSecret`) e as redes dele na NetworkPolicy |
| `executions.egress.allowedInternalNetworks` | `RAPA_REDES_INTERNAS_PERMITIDAS` | os portais internos que os robôs alcançam (na guarda e na NetworkPolicy), CIDRs estreitos |
| `executions.egress.extraInternalNetworks` | | redes internas a mais que os robôs nunca alcançam (o bloco IPv6 da VPC; as de IPv4 também pelo NAT64) |
| `executions.admissionPolicy.enabled` | | a `ValidatingAdmissionPolicy` do que a web e o despachante criam no namespace das execuções |
| `operation.*` | `RAPA_OWNER_DATABASE_URL`, `RAPA_BAO_PAPEL` (`vault.roles.operation`) | o modelo do pod da operação: o disco da exportação (`workSizeLimit`) e o prazo dele |
| `vault.*` | `RAPA_COFRE` (só no despachante e na operação), `RAPA_BAO_ENDERECO`, `RAPA_BAO_CA`, `RAPA_BAO_KV`, `RAPA_BAO_TRANSIT`, `RAPA_BAO_AUTH`, `RAPA_BAO_PAPEL` (`vault.roles`: um papel por componente, os do `preparar.sh`), `RAPA_BAO_TOKEN_SA` | o OpenBao; o token da conta de serviço é projetado com a audiência `vault.tokenAudience` (a do `preparar.sh`, `openbao`) |
| `storage.*` | `RAPA_ARMAZENAMENTO`, `RAPA_S3_ENDERECO`, `RAPA_S3_REGIAO`, `RAPA_S3_BUCKET`, `RAPA_S3_CA`, `RAPA_S3_ESTILO_CAMINHO`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | os artefatos no S3 (sempre `s3` no cluster) |
| `keys.*` | `RAPA_CHAVES`, `RAPA_CHAVE_ARTEFATOS` | as chaves dos artefatos (`openbao` em produção) |
| `observability.*` | `RAPA_METRICAS_PORTA`, `RAPA_METRICAS_POR_EMPRESA`, `RAPA_LOG_JSON`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, `RAPA_GLITCHTIP_DSN` | métricas (só no pod; o `VMPodScrape` opcional), logs em JSON, traços e o GlitchTip |
| `modelHub.*` | `RAPA_LLM_BASE_URL`, `RAPA_LLM_MODEL`, `RAPA_LLM_API_KEY`, `RAPA_LLM_CAPACIDADES` | o hub do modelo, com a matriz da sonda num ConfigMap |
| `publishing.*` | `RAPA_CHAVE_WEBHOOKS`, `RAPA_AGENDA_*`, `RAPA_AGENDAS_POR_EMPRESA`, `RAPA_WEBHOOKS_POR_SEGUNDO`, `RAPA_LOTE_*` | agendas, webhooks e lotes |
| `repairCandidates`, `a1WarnDays`, `evidence.*` | `RAPA_CONSERTO_CANDIDATOS`, `RAPA_A1_AVISO_DIAS`, `RAPA_VIDEO_EM_FALHAS`, `RAPA_RETENCAO_*` | conserto, aviso do A1, vídeo e retenção |
| `web.sessions.*` | `RAPA_SESSOES_POR_EMPRESA`, `RAPA_SESSOES_MAX`, `RAPA_SESSAO_OCIOSA_S`, `RAPA_SESSAO_MAX_S` | as sessões de gravação |
| `dispatcher.concurrency` | `RAPA_CONCORRENCIA` | execuções ao mesmo tempo em cada réplica |
| `gateway.*` | `FORWARDED_ALLOW_IPS` | as `HTTPRoute`, as redes do Envoy (obrigatórias), o prazo dos pedidos comuns (`requestTimeout`; sem prazo só os websockets e o `/mcp`) e a afinidade da web |
| `network.*` | | as NetworkPolicies: DNS, coletor de métricas e os destinos de saída |

`RAPA_EXECUTOR` (no despachante) e `RAPA_SESSOES_EXECUTOR` (na web) são sempre `kubernetes` no cluster. O que cada variável faz, com o padrão dela, está em 1.11.

### 1.5 Backup e restauração do banco

O Cluster arquiva o WAL continuamente pelo plugin Barman Cloud no bucket `rapa-backups` (o `ObjectStore` `rapa-banco-backup`), com um backup de base diário (`ScheduledBackup` `rapa-banco-diario`, 03:00 UTC) e retenção de 30 dias (`database.cnpg.backup.retentionPolicy`). Para conferir:

```bash
kubectl -n rapa get backups.postgresql.cnpg.io
kubectl -n rapa get cluster rapa-banco -o jsonpath='{.status.conditions}'
```

**Teste de restauração semanal** (o backup só vale se volta): `deploy/banco/testar-restauracao.sh` sobe um Cluster novo a partir dos backups num namespace à parte (`rapa-restauracao`), com o dono que está nos backups (`rapa_owner`), confere que o banco abre (a revisão da migração e as contagens) e apaga tudo. O banco de produção não é tocado: o Cluster restaurado só lê os backups e não arquiva WAL. Agende toda semana, numa máquina da operação com o `kubectl` do cluster (o cron do comentário no topo do script), e registre o resultado: a última linha diz o que foi conferido e quanto tempo levou (é o tempo de recuperação de verdade); uma saída diferente de 0 é uma restauração que falhou e precisa de alguém.

**Restaurar de verdade** (perda do banco ou uma migração a desfazer). O Cluster novo vem da própria release do banco, com outro nome: assim ele ganha a NetworkPolicy dele (o namespace nega tudo, e a do Cluster antigo só escolhe os pods dele), e as NetworkPolicies da aplicação passam a apontar para ele pelo mesmo `database.cnpg.name`.

1. Pare a aplicação. Com o Argo CD, desligue primeiro o sync automático da raiz, que é dona das duas Applications e, com o `selfHeal`, desfaz em segundos uma mudança nelas; depois o das duas filhas (o `selfHeal` da `rapa` desfaria o `scale`):

   ```bash
   argocd app set rapa-raiz --sync-policy none
   argocd app set rapa --sync-policy none
   argocd app set rapa-banco --sync-policy none
   ```

   Depois, com ou sem o Argo CD, `kubectl -n rapa scale deployment rapa-web rapa-api rapa-despachante --replicas=0`.
2. Nos valores do ambiente (os mesmos das duas releases; com o Argo CD, no repositório de configuração que as duas Applications leem):

   ```yaml
   database:
     host: rapa-banco-2-pooler-rw      # ou rapa-banco-2-rw, sem o Pooler
     caSecret:
       name: rapa-banco-2-ca
     cnpg:
       name: rapa-banco-2
       recovery:
         enabled: true
         source: rapa-banco             # o Cluster de origem (o nome do servidor nos backups)
         targetTime: ""                 # ou um instante, RFC 3339 ("2026-11-20T03:00:00Z")
   ```

   O Cluster novo nasce dos backups da origem (o `ObjectStore` `rapa-banco-2-origem`, só leitura), com o mesmo dono (`rapa_owner`) e os mesmos papéis, e arquiva o WAL na pasta dele (`rapa-banco-2`): o WAL dos dois não se mistura. O chart recusa o mesmo nome da origem.
3. Atualize a release do banco e espere o Cluster novo e os papéis:

   ```bash
   helm upgrade rapa-banco deploy/helm/rapa -n rapa -f valores-do-ambiente.yaml -f deploy/helm/rapa/values-banco.yaml
   kubectl -n rapa wait cluster/rapa-banco-2 --for=condition=Ready --timeout=60m
   kubectl -n rapa get cluster rapa-banco-2 -o jsonpath='{.status.managedRolesStatus.byStatus.reconciled}'
   ```

   Com o Argo CD não há release do Helm (ele renderiza o chart e aplica): em vez do `helm upgrade`, faça o commit dos valores e rode `argocd app sync rapa-banco`, e espere do mesmo jeito.

   O Cluster antigo fica (o chart nunca o apaga), sem NetworkPolicy própria (isolado pelo nega-tudo) e sem o ObjectStore dele (com o Argo CD, cujo prune do banco é desligado, os dois também ficam: apague-os junto com ele): confira o novo antes de apagar o antigo à mão (`kubectl -n rapa delete cluster rapa-banco`). O Cluster novo faz um backup de base logo que fica pronto; os backups do antigo ficam no bucket, na pasta dele (`rapa-banco`), sem retenção: apague-a quando não precisar mais deles.
4. Atualize a release da aplicação (a migração confere a revisão; a web, a api, o despachante, a migração e a operação passam a sair para o `rapa-banco-2`): `helm upgrade rapa deploy/helm/rapa -n rapa -f valores-do-ambiente.yaml`, ou, com o Argo CD, a aplicação pelo sync e o sync automático de volta, a raiz por último (ela devolve às filhas o `syncPolicy` do repositório):

   ```bash
   argocd app sync rapa
   argocd app set rapa-raiz --sync-policy automated --self-heal
   ```
5. Confira o que as execuções em andamento na hora da perda deixaram: as que tinham começado um passo que grava ficam para uma pessoa conferir o sistema de destino.

### 1.6 Cofre (OpenBao)

- **Iniciar uma vez**, com 5 custodiantes e quórum de 3: `bao operator init -key-shares=5 -key-threshold=3 -pgp-keys=c1.asc,c2.asc,c3.asc,c4.asc,c5.asc`. Cada parte sai cifrada para a chave PGP de um custodiante. Com o token raiz, e antes de revogá-lo: o `preparar.sh`; o método de entrada de quem administra o cofre (por exemplo, o OIDC do Keycloak), com uma política de administração (as políticas, os métodos de entrada e a montagem do transit, para a rotação abaixo); e o dos custodiantes das credenciais, com uma política por empresa (`deploy/openbao/custodiante.sh <uuid da empresa>`, ligada só à identidade do custodiante dela; `deploy/openbao/README.md`, "Custodiantes"). Só então o token raiz é revogado (`bao token revoke`): daí em diante, só essas identidades escrevem no cofre, e um token raiz novo só se gera com o quórum (`bao operator generate-root`).
- **Desbloquear** (depois de reiniciar um pod do OpenBao ou do cluster): três custodiantes, cada um da própria máquina, rodam `bao operator unseal` com a parte dele, em cada pod do OpenBao (`kubectl -n cofre port-forward openbao-0 8200`). Enquanto estiver selado, veja 1.8.
- **Rotação**: as partes dos custodiantes com `bao operator rekey` (quando um custodiante sai), a chave de cifra interna com `bao operator rotate`, e a chave transit de uma empresa com `bao write -f rapa-transit/keys/empresa-<uuid>/rotate` (as versões antigas continuam decifrando; o RaPA guarda a versão de cada chave embrulhada). Os tokens das contas de serviço são projetados e trocados sozinhos pelo Kubernetes.
- Os segredos das credenciais ficam em `rapa/data/empresas/<empresa>/<caminho>`; quem os grava é o custodiante, direto no OpenBao, com a política da empresa dele (`rapa-custodiante-<empresa>`, de `deploy/openbao/custodiante.sh`: grava e lista só ali, nunca lê o valor nem alcança outra empresa; nunca uma política de `empresas/*`). Cada empresa nova ganha a dela na entrada (Parte 2). O RaPA só guarda a referência.

### 1.7 Rotação de `RAPA_CHAVE_WEBHOOKS`

A chave de cada webhook é derivada de `RAPA_CHAVE_WEBHOOKS` com o sal do webhook: trocar a chave do servidor invalida a de todos os webhooks de todas as empresas ao mesmo tempo. Faça numa janela combinada com as empresas:

1. Gere a chave nova (`umask 077; openssl rand -base64 48 | tr -d '\n' > /tmp/chave`) e troque o Secret `rapa-webhooks` (`kubectl -n rapa create secret generic rapa-webhooks --from-file=chave=/tmp/chave --dry-run=client -o yaml | kubectl apply -f -`).
2. Reinicie a web e a api juntas: `kubectl -n rapa rollout restart deployment rapa-web rapa-api`.
3. Cada empresa troca a chave de cada webhook (tela Webhooks ou `POST /v1/robos/{id}/webhooks/{wid}/trocar-chave`) e entrega a nova a quem chama. Até isso, as chamadas são recusadas (401), nunca aceitas com a chave velha.

Troque também quando houver suspeita de vazamento.

### 1.8 Incidentes

- **Despachante parado.** `/saude` da réplica responde 503 com `despachante_parado` ou `despachante_com_falha` e as etapas que falham (só nomes); o probe de vida reinicia o pod depois de 5 minutos assim. Veja `kubectl -n rapa logs deploy/rapa-despachante` (o tipo do erro) e se o banco e o papel `rapa_dispatcher` respondem. A fila só espera: nada se perde. Execuções que estavam rodando numa réplica que caiu voltam para a fila quando a concessão vence se nenhum passo que grava tinha começado; senão falham (ou ficam indeterminadas) para uma pessoa conferir.
- **Fila cheia.** A REST responde 429 (`limite_excedido`): a fila da empresa chegou ao limite dela (`--fila`). Confira quantas rodam e esperam por empresa (`/v1/empresa/consumo` e as métricas da fila). Mais vazão: mais réplicas do despachante ou `dispatcher.concurrency`, as vagas da plataforma (`platform_settings.total_browser_slots`, como o dono das tabelas) e a capacidade dos nós de execução (e a cota do namespace, se houver).
- **A1 vencido.** As execuções do robô falham com `certificado_a1_bloqueado`, sem lançar nada. O aviso aparece antes (`RAPA_A1_AVISO_DIAS`), na tela Credenciais e em `/v1/credenciais/vencimentos`. O custodiante grava o certificado novo no cofre e renova a validade (`PATCH /v1/credenciais/{id}`); com a impressão nova, autoriza o robô de novo.
- **OpenBao selado.** `/saude/pronto` responde 503 com o OpenBao, e ela é a prontidão da web, da api e do despachante: todas as réplicas da web e da api saem dos Services e o Envoy responde 503 a tudo (interface, REST, MCP e webhooks; quem chama um webhook recebe 503 e precisa tentar de novo). É de propósito: sem a chave da empresa (no transit), nenhum artefato abre e nenhuma execução com credenciais anda, e um pod pela metade não recebe tráfego. Os pods continuam vivos (`/saude`), a fila espera e as execuções que já rodam terminam; as novas com credenciais falham com o segredo indisponível (sem lançar o navegador). Desbloqueie com o quórum (1.6); nada precisa ser reiniciado, e tudo volta sozinho em segundos. Durante o incidente, não atualize o chart: os pods novos não ficam prontos e a troca para.
- **S3 fora.** O mesmo: `/saude/pronto` responde 503 com o S3, a web e a api saem dos Services e o Envoy responde 503 a tudo até o S3 voltar. Confira os pods e os discos do SeaweedFS, e a NetworkPolicy (`network.egress.s3`). Os arquivos de passagem ficam no `emptyDir` do despachante até subirem.
- **Hub do modelo fora.** Só a criação e o conserto assistidos perdem as propostas do modelo (a revisão mostra só as regras, com um aviso; no conserto, a pessoa escolhe sozinha). As execuções não usam o modelo e seguem. Confira o hub e `network.egress.modelHub`.
- **Pod de execução que não sobe.** A execução falha com `executor_nao_iniciou`. Veja os pods: `kubectl -n rapa-execucoes get pods -l app.kubernetes.io/name=rapa-executor` e `kubectl -n rapa-execucoes describe pod ...`:
  - `RuntimeClass "gvisor" not found` ou `no runtime handler`: a RuntimeClass não foi aplicada ou o nó não tem o `runsc` (1.2, passo 1);
  - `ImagePullBackOff`: o digest não existe no registro ou falta o `rapa-registro` no namespace das execuções;
  - `Unschedulable` (CPU, memória, cota): faltam nós de execução, ou o `ResourceQuota` do namespace chegou ao limite;
  - `forbidden` nos logs do despachante: o RBAC (a RoleBinding da conta `rapa-despachante`) ou a NetworkPolicy para a API do Kubernetes (`network.egress.kubernetesApi`);
  - `denied request: ValidatingAdmissionPolicy 'rapa-execucoes-pods-de-uso-unico'` nos logs: o pod que o lançador montou não é o de uso único (a imagem não é a de `images.executor` nem uma de `images.executor.previousDigests`, a RuntimeClass, a conta ou o comando); a mensagem diz qual regra;
  - o pod sobe mas a execução não anda: a NetworkPolicy do canal (o despachante na porta 8081) ou o `RAPA_CANAL_ENDERECO`;
  - a execução falha com `segredo_indisponivel` sem nenhum pod: a credencial não está registrada na área do robô, o segredo ou um campo não está no KV (`rapa/data/empresas/<empresa>/<caminho>`), o certificado A1 não é um PFX em base64, ou o OpenBao não respondeu (a mensagem diz o que falta, só pelos nomes).
- **Despachante que não sobe.** `rapa.server: configuração inválida: ...` no log, sem traceback e sem os valores: um `RAPA_CANAL_ENDERECO` sem `{ip}` (um Service), a porta do canal igual à da saúde ou à das métricas, a imagem do executor que falta, ou um valor do cofre e do S3 fora da regra (1.11).

### 1.9 Conferir a guarda de saída

Depois de instalar (e a cada mudança de rede), rode o pod de teste com o mesmo rótulo, a mesma conta e as mesmas regras dos pods de execução, com a imagem do executor:

```bash
kubectl apply -f deploy/rede/teste-de-saida.yaml      # troque a imagem pela do ambiente
kubectl -n rapa-execucoes logs -f teste-de-saida
kubectl -n rapa-execucoes delete pod teste-de-saida
```

Cada alvo interno (o metadata da nuvem em IPv4, em IPv6 e pelo NAT64, a API do Kubernetes, o banco, a web, uma rede privada também pelo NAT64) precisa sair como `ok: fechado`, e os externos como `ok: abriu` (sem internet direta, os externos só abrem pelo proxy). Um interno com `ERRO: abriu` (sobretudo o banco, a web ou a API do Kubernetes, que nenhuma regra do chart abre) é quase sempre o CNI que não aplica as NetworkPolicies (no EKS, o VPC CNI sem `enableNetworkPolicy`, 1.2 passo 1): nenhuma empresa usa o RaPA até o teste passar. Some aos internos, em `ALVOS_DO_AMBIENTE`, os portais que não estão em `RAPA_REDES_INTERNAS_PERMITIDAS`; num cluster IPv6, também o endereço IPv6 de um pod do plano de controle (`kubectl -n rapa get pod -o wide`) e um da VPC, e em `ALVOS_EXTERNOS_IPV6` um destino da internet em IPv6. O pod de teste é criado por quem opera, não pelas contas do RaPA: a política de admissão dos pods de uso único não vale para ele. Ele leva o rótulo dos pods de execução sem uma tentativa: a varredura de órfãos do despachante o apaga 15 minutos depois de criado (antes disso, ele fica para a conferência). A guarda dentro do pod (a do navegador) se confere com um robô de teste cujo passo abre um domínio fora de `allowed_domains`: o passo falha com `dominio_nao_permitido`, também por WebSocket.

### 1.10 O CLI no cluster

Os comandos da Parte 2 rodam num pod da operação, criado do `PodTemplate` `rapa-operacao` que o chart guarda (nada roda sozinho), e nunca nos pods que servem tráfego: a api não lê o cofre (`RAPA_COFRE=nenhum`) e o papel dela no OpenBao só decifra, então `empresa apagar` ali não destruiria os segredos nem a chave `empresa-<uuid>` (ou falharia fechado), e uma exportação grande no `/tmp` dela (1 GiB) faria o kubelet despejar o pod. O pod da operação tem:

- a conta de serviço `rapa-operacao` (nenhuma permissão no Kubernetes, nenhum token montado) e o token projetado para o papel `rapa-operacao` do OpenBao (`vault.roles.operation`; a política `deploy/openbao/politicas/rapa-operacao.hcl`: apagar os metadados dos segredos da empresa e a chave transit dela, e decifrar os artefatos para a exportação), com `RAPA_COFRE` e `RAPA_CHAVES` do ambiente;
- `RAPA_DATABASE_URL` (o papel `rapa_app`) e `RAPA_OWNER_DATABASE_URL` (o dono das tabelas, com a senha do Secret `rapa-banco-dono` pela expansão `$(VAR)`: ela nunca passa pela linha de comando);
- o S3 dos artefatos e um disco próprio para a exportação (`/trabalho`, `operation.workSizeLimit`, 50Gi por padrão), longe dos pods que servem tráfego: o `TMPDIR` aponta para ele, então a pasta de passagem da exportação (as tabelas e os artefatos decifrados, antes do `.tar.gz`) também fica ali, nunca no `/tmp` de 1 GiB; o disco precisa caber a passagem e o arquivo final juntos;
- uma NetworkPolicy que só sai para o banco, o S3 e o OpenBao, e o prazo de 4 horas (`operation.activeDeadlineSeconds`).

```bash
kubectl -n rapa get podtemplate rapa-operacao -o json \
  | jq '{apiVersion: "v1", kind: "Pod", metadata: (.template.metadata + {name: "rapa-operacao"}), spec: .template.spec}' \
  | kubectl -n rapa create -f -
kubectl -n rapa wait pod/rapa-operacao --for=condition=Ready --timeout=5m
op() { kubectl -n rapa exec rapa-operacao -- python -m rapa.control.admin "$@"; }

op empresa consumo ID_DA_EMPRESA --mes 2026-11
op empresa congelar ID_DA_EMPRESA --motivo "contrato encerrado" --por "$USER"
op empresa exportar ID_DA_EMPRESA --saida /trabalho/empresa.tar.gz --por "$USER"
kubectl -n rapa cp rapa-operacao:/trabalho/empresa.tar.gz ./empresa.tar.gz
op empresa apagar ID_DA_EMPRESA --confirmar "Nome da empresa" --por "$USER"

kubectl -n rapa delete pod rapa-operacao      # o disco da exportação some com o pod
```

O pod não leva o rótulo `app.kubernetes.io/instance`: o Argo CD (com o rastreio por rótulo) não o toma como recurso da Application nem o apaga no meio do trabalho. Com um registro privado, o `rapa-registro` já está no modelo. A exportação leva os artefatos decifrados: o arquivo sai do cluster só para quem vai entregá-lo, e o pod é apagado logo depois.

### 1.11 As variáveis do serviço

As variáveis de `python -m rapa.server` que o deploy no Kubernetes acrescentou (as de antes estão na docstring de `rapa.server.ServerSettings.from_env` e no README). Todas são conferidas na partida: um valor fora da regra para o processo com `configuração inválida`, sem traceback e sem repetir o valor. Os valores do chart que as preenchem estão em 1.4.

| Variável | Padrão | O que faz |
|---|---|---|
| `RAPA_EXECUTOR` | `local` | onde rodam as execuções: `local` (um processo por tentativa) ou `kubernetes` (um pod de uso único por tentativa, que o despachante cria e segue pelo canal) |
| `RAPA_SESSOES_EXECUTOR` | o de `RAPA_EXECUTOR` | o mesmo para as sessões de gravação (`kubernetes`: o host de sessão num pod, com a mesma imagem e o mesmo namespace) |
| `RAPA_K8S_NAMESPACE_EXECUCOES` | `rapa-execucoes` | o namespace dos pods de uso único |
| `RAPA_K8S_IMAGEM_EXECUTOR` | (obrigatória no modo `kubernetes`) | a imagem do executor (alvo `executor`) |
| `RAPA_K8S_RUNTIME_CLASS` | `gvisor` | a RuntimeClass dos pods; vazia, nenhuma (só kind e desenvolvimento) |
| `RAPA_K8S_CPU`, `RAPA_K8S_MEMORIA` | `1`, `2Gi` | os limites (iguais aos pedidos) de cada pod |
| `RAPA_K8S_SA_EXECUTOR` | (a conta `default`) | a conta de serviço dos pods, sem permissão e sem token montado (no chart, `rapa-executor`) |
| `RAPA_K8S_PULL_SECRET` | (nenhum) | o Secret do registro no namespace das execuções |
| `RAPA_K8S_NODE_SELECTOR`, `RAPA_K8S_TOLERATIONS` | (nenhum) | onde os pods rodam, em JSON (`{"rótulo": "valor"}` e uma lista como a do Kubernetes) |
| `RAPA_CANAL_PORTA` | `8081` | onde o despachante ouve o canal dos pods (só no modo `kubernetes`); nunca a porta do serviço nem a das métricas |
| `RAPA_CANAL_ENDERECO` | `http://{ip}:<RAPA_CANAL_PORTA>` | o endereço que os pods usam para falar com o despachante; precisa de `{ip}`, que vira o IP do pod do despachante (um Service é recusado: cada réplica tem os bilhetes das tentativas dela) |
| `RAPA_PROXY_SAIDA` | (nenhum) | o proxy HTTP CONNECT corporativo para a internet dos navegadores (`http://host:porta`; com senha, `http://usuario:senha@host:porta` de um Secret, mandados no `Proxy-Authorization: Basic`) |
| `RAPA_REDES_INTERNAS_PERMITIDAS` | (nenhuma) | CIDRs, separados por vírgula, que os robôs e as sessões alcançam além da internet (os portais internos) |
| `RAPA_COFRE` | `nenhum` (`dev` com `RAPA_DEV=1` e `RAPA_SEGREDOS_DEV`) | de onde vêm os segredos das credenciais: `nenhum`, `dev` (o arquivo de `RAPA_SEGREDOS_DEV`, só em desenvolvimento) ou `openbao` |
| `RAPA_BAO_ENDERECO` | (obrigatório com o OpenBao) | o OpenBao (`https://` fora do desenvolvimento; o serviço ativo: o cliente não segue redirecionamento) |
| `RAPA_BAO_CA` | (os CAs do sistema) | o CA que confere o TLS do OpenBao |
| `RAPA_BAO_KV`, `RAPA_BAO_TRANSIT` | `rapa`, `rapa-transit` | as montagens do KV v2 e do transit |
| `RAPA_BAO_AUTH`, `RAPA_BAO_PAPEL` | `kubernetes`, (obrigatório) | o método de entrada e o papel do componente (os do `preparar.sh`) |
| `RAPA_BAO_TOKEN_SA` | `/var/run/secrets/rapa/bao/token` | o token projetado da conta de serviço, com a audiência do OpenBao |
| `RAPA_BAO_TOKEN_ARQUIVO` | (nenhum) | um token do OpenBao num arquivo, em vez da entrada pelo método kubernetes: só em desenvolvimento |
| `RAPA_ARMAZENAMENTO` | `local` | onde ficam os artefatos: `local` (a pasta `RAPA_PASTA_ARTEFATOS`) ou `s3` (cifrados, e a pasta vira só a de passagem); no cluster, sempre `s3` |
| `RAPA_S3_ENDERECO`, `RAPA_S3_BUCKET` | (obrigatórios com `s3`) | o S3 (o SeaweedFS; `https://` fora do desenvolvimento) e o bucket |
| `RAPA_S3_REGIAO` | `us-east-1` | a região da assinatura |
| `RAPA_S3_CA` | (os CAs do sistema) | o CA que confere o TLS do S3 |
| `RAPA_S3_ESTILO_CAMINHO` | `1` | o bucket no caminho, não no nome do host (o SeaweedFS) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | (obrigatórias com `s3`) | as credenciais do S3, de um Secret; o cliente nunca procura outras |
| `RAPA_CHAVES` | `openbao` com `RAPA_COFRE=openbao`; senão `local` | a chave de cada empresa, que embrulha a chave de cada execução: `openbao` (transit, `empresa-<uuid>`) ou `local` |
| `RAPA_CHAVE_ARTEFATOS` | (sorteada por processo, só em desenvolvimento) | a chave mestra das chaves locais, 32 caracteres ou mais, só para desenvolvimento e testes |
| `RAPA_METRICAS_PORTA` | `9100` | a porta do `GET /metrics`, no mesmo endereço do serviço (o `--host`); `0` desliga; nunca a porta do serviço |
| `RAPA_METRICAS_POR_EMPRESA` | `0` | `1` põe o rótulo `empresa` (o uuid) nas execuções concluídas, na fila e nos avisos do A1 |
| `RAPA_LOG_JSON` | `0` | `1`: um objeto JSON por linha (`hora`, `nivel`, `logger`, `mensagem` e `rastreio`), também nas linhas do uvicorn |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | (nenhum: nada de traços) | liga os traços OpenTelemetry por OTLP/HTTP (o extra `observability`); `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` troca só o dos traços |
| `OTEL_SERVICE_NAME` | `rapa` | o nome do serviço nos traços (no chart, o do componente) |
| `RAPA_GLITCHTIP_DSN` | (nenhum) | as exceções não tratadas, limpas, no GlitchTip (o extra `observability`); o DSN tem a chave do projeto: de um Secret |

No cluster, a pasta dos arquivos de entrada (`RAPA_PASTA_ARQUIVOS`) não é montada no despachante (o disco de um pod não chega aos outros): um robô com passo de envio falha com "nenhum diretório de arquivos de entrada foi configurado".

### 1.12 Observabilidade e saúde

As regras estão em `docs/CONTRIBUINDO.md` ("Regras da observabilidade e da saúde"); as variáveis, em 1.11. Sondas: vida em `/saude`, prontidão em `/saude/pronto` (503 com os nomes do que falhou: `banco`, `banco_despachante`, `despachante` e, com o cofre e o armazenamento de objetos, `openbao` e `s3`). No despachante, as duas ficam na porta da saúde (`dispatcher.healthPort`), nunca na do canal. As métricas (`GET /metrics` na `RAPA_METRICAS_PORTA`, nunca pelo gateway) o coletor (VictoriaMetrics, o `VMPodScrape` opcional do chart) lê direto do pod. Para conferir à mão, num port-forward do pod:

```bash
curl -s http://127.0.0.1:8000/saude/pronto     # no despachante, a porta 8080
curl -s http://127.0.0.1:9100/metrics | grep -E '^rapa_(fila|despachante_saudavel|a1_)'
```

Alertas que valem a pena: `rapa_despachante_saudavel == 0`; `rapa_despachante_etapa_ok == 0` por mais de alguns minutos; `rapa_fila_espera_maxima_segundos` alta (fila cheia); `time() - rapa_banco_lido_em_segundos > 120` (o despachante não lê o banco); `rapa_a1_dias_para_vencer < 7`; `rapa_a1_em_aviso{tipo="vencido"} > 0`; aumento de `rapa_webhook_entregas_total{resultado="assinatura_invalida"}`; `rapa_http_pedidos_total{codigo=~"5.."}`. Com mais de um despachante, a leitura da fila é a mesma em todos (`max`) e as execuções concluídas somam (`sum`).

### 1.13 Documentação da API

A documentação da API REST fica desligada por padrão. Para ligar, suba o serviço com `RAPA_DOCS_API=1`:

- `/v1/docs`: o Swagger UI, com os arquivos servidos pelo próprio serviço (nenhum CDN; funciona onde o navegador não alcança a internet), sem o validador público do Swagger (as licenças dele e as do código de terceiros que o bundle junta vêm junto, em `src/rapa/control/static/swagger-ui/`);
- `/v1/openapi.json`: o documento OpenAPI da API REST (só as rotas de `/v1`; a interface e o MCP ficam de fora).

As duas abrem sem token e mostram só as rotas e os formatos, nunca dados. Para chamar a API pela página, clique em **Authorize**, informe o token (sem a palavra `Bearer`: a página manda `Authorization: Bearer ...`) e use **Try it out** na rota. O token fica só na página aberta (o navegador não o guarda; recarregar a página o esquece), e cada chamada vale como qualquer outra chamada da API, com os papéis do dono do token: um `POST` feito ali cria de verdade. A entrega de webhook (`POST /v1/webhooks/{id}`) não usa o token, e sim a assinatura nos cabeçalhos. Ligue onde a documentação for útil (desenvolvimento, homologação, a rede interna) e deixe desligada onde não houver motivo para mostrar as rotas. O OpenAPI dos robôs publicados de quem chama (`GET /v1/openapi-robos.json`, com token) não depende dessa opção.

## Parte 2. Operação das empresas

Roteiro curto para o time da plataforma: a entrada de uma empresa, congelar, exportar e apagar. Os comandos são de `python -m rapa.control.admin` (no cluster, no pod da operação, como em 1.10). As regras completas estão nas docstrings de `rapa.control.services.lifecycle`, `rapa.control.services.company` e da migração `v0007_operacao_empresas`.

Duas URLs de banco:

- a do papel da aplicação (`rapa_app`), em `--url` ou `RAPA_DATABASE_URL`, para a entrada e o consumo;
- a do dono das tabelas (`rapa_owner`), em `--url-dono` ou `RAPA_OWNER_DATABASE_URL`, para congelar, descongelar, exportar e apagar. Com a URL da aplicação no lugar, o comando recusa com uma mensagem clara.

`--por` diz quem pediu e vai para a auditoria (padrão: o usuário do sistema). Nenhum comando imprime segredos.

Exportar e apagar usam o mesmo armazenamento, as mesmas chaves e o mesmo cofre do servidor (`RAPA_ARMAZENAMENTO`, `RAPA_S3_*` com `AWS_ACCESS_KEY_ID` e `AWS_SECRET_ACCESS_KEY`, `RAPA_CHAVES`, `RAPA_COFRE` e `RAPA_BAO_*`; veja as regras do cofre em [CONTRIBUINDO.md](CONTRIBUINDO.md)). Com o OpenBao, o comando entra com o papel `rapa-operacao` (`RAPA_BAO_PAPEL=rapa-operacao`, no pod da operação de 1.10, com a conta de serviço `rapa-operacao`; o preparo está em `deploy/openbao/preparar.sh`), que desembrulha as chaves das execuções (exportar) e apaga os segredos e a chave transit da empresa (apagar), sem nunca ler o valor de um segredo.

### 1. Entrada

1. Crie a Organização da empresa no Keycloak e anote o id dela (ou o apelido).
2. Crie a empresa, com quem vai administrá-la e as primeiras áreas, numa transação só:

```bash
uv run python -m rapa.control.admin empresa criar "Nome da empresa" --organizacao ID_DA_ORGANIZACAO --admin SUB_DO_ADMIN --nome-admin "Nome da pessoa" --area "Fiscal" --area "Compras" --vagas 2 --fila 1000 --emprestimo 2.0
```

O comando imprime o id da empresa. A organização fica ligada à empresa no banco (`companies.idp_organization`): o serviço passa a aceitar os tokens dela em até 60 segundos, sem reiniciar. O mapa estático `RAPA_ORGANIZATION_ALIASES` continua valendo; se ele e o banco discordarem sobre uma organização, o login é recusado.

O formato diz se `--organizacao` é o id ou o apelido: um valor no formato de UUID é sempre o id da organização (guardado em minúsculas, como o Keycloak o põe no token) e é comparado só com o id do token; qualquer outro valor é o apelido e é comparado só com o apelido. Prefira o id. Uma organização cujo apelido tem o formato de UUID só pode ser ligada pelo id.

3. No OpenBao, quem administra o cofre cria a política do custodiante da empresa (`deploy/openbao/custodiante.sh ID_DA_EMPRESA`) e a liga só à identidade dele (1.6): sem ela, ninguém grava os segredos das credenciais da empresa, e os robôs com credenciais falham com `segredo_indisponivel`.
4. Quem entra pela primeira vez com um token dessa organização vira usuário sem papel nenhum e só vê o pedido para um administrador dar um papel. O administrador da empresa dá os papéis na tela **Empresa** (ou em `PUT /v1/empresa/usuarios/{id}/papeis`).

### 2. Consumo do mês

```bash
uv run python -m rapa.control.admin empresa consumo ID_DA_EMPRESA --mes 2026-11
```

`--json` imprime o mesmo relatório da API (`GET /v1/empresa/consumo?mes=2026-11`). O mês é o do fuso `America/Sao_Paulo`, pela data em que cada execução foi pedida.

### 3. Saída

A saída é sempre nesta ordem: congelar, exportar, entregar o arquivo, apagar.

#### Congelar

```bash
uv run python -m rapa.control.admin empresa congelar ID_DA_EMPRESA --motivo "contrato encerrado"
```

Daí em diante a empresa lê tudo, mas nada novo começa (a API responde 423, `empresa_congelada`): nem execução (REST, MCP, interface, agenda, webhook, lote), nem versão, publicação (também a aprovação de um robô com certificado A1), agenda, webhook, lote, sessão de gravação ou conserto, nem registrar, renovar ou autorizar um certificado. O banco também recusa a linha nova nessas tabelas, mesmo de um caminho que não passe pelos serviços. Revogar uma credencial ou uma autorização do custodiante continua valendo (só tira acesso), e as evidências continuam abertas para quem vê a execução. As execuções que estavam na fila são canceladas (código `empresa_congelada`); as que estavam rodando terminam normalmente (o despachante nem pega o que sobrar na fila: a conferência do certificado A1 nem chega a rodar). As agendas registram cada ocorrência como pulada (`empresa_congelada`). Enquanto estiver congelada, nada da empresa expira na retenção (telas, vídeos, arquivos baixados, retratos, chaves de idempotência e a partição de eventos em que ela tem eventos) e ela não recebe aviso de vencimento do certificado: tudo fica como estava para a exportação. Para voltar:

```bash
uv run python -m rapa.control.admin empresa descongelar ID_DA_EMPRESA
```

#### Exportar

```bash
uv run python -m rapa.control.admin empresa exportar ID_DA_EMPRESA --saida empresa.tar.gz
```

O arquivo nasce com permissão 0600 e nunca sobrescreve outro. Links simbólicos nas pastas da empresa nunca são seguidos (nem um arquivo trocado por um link no meio da exportação), e o que entra no arquivo é exatamente o que o manifesto confere. Dentro dele: `manifesto.json` (o SHA-256 e o número de linhas de cada arquivo), `tabelas/<tabela>.jsonl` (uma linha por registro, de toda tabela com `company_id`, mais a da empresa: também as credenciais, as autorizações do custodiante, as aprovações e os avisos de vencimento do A1, e as execuções com as marcas de artefatos expirados) os arquivos de entrada da empresa em `arquivos/` (de `RAPA_PASTA_ARQUIVOS/<empresa>` ou `--pasta-arquivos`) e os artefatos em `artefatos/<execução>/<tentativa>/<nome>` (as telas, os vídeos e os arquivos baixados que a retenção ainda não apagou, lidos pelo armazenamento configurado: a pasta `RAPA_PASTA_ARTEFATOS` ou `--pasta-artefatos` e, no S3, os objetos decifrados, como a pessoa os vê pela API; um artefato que não abre fica de fora e é contado em `artefatos_ilegiveis` no manifesto). Material de chave nunca sai: o sal dos webhooks e as chaves embrulhadas das execuções (`run_keys.wrapped_key`) vêm como `null`; de uma credencial sai só a referência ao cofre e os metadados (o valor nunca passa pelo RaPA). Confira o manifesto antes de entregar:

```bash
tar -xzf empresa.tar.gz && sha256sum tabelas/*.jsonl
```

#### Apagar

Só uma empresa congelada, sem execuções rodando nem sessões de gravação abertas (no prazo: uma sessão que a réplica da web que caiu deixou aberta no banco para de contar no prazo máximo dela, `RAPA_SESSAO_MAX_S`), e com o nome exato:

```bash
uv run python -m rapa.control.admin empresa apagar ID_DA_EMPRESA --confirmar "Nome da empresa"
```

Numa transação só, o comando apaga todas as linhas da empresa, das filhas para as mães (inclusive as do certificado A1, as das evidências e as chaves das execuções), e por fim a empresa; ainda dentro dela, apaga os artefatos dela no armazenamento e, com o OpenBao, os segredos (todas as versões, em `empresas/<empresa>/`) e a chave transit `empresa-<uuid>` (sem a chave da empresa, nada que tenha sobrado num backup do S3 abre), e grava em `platform_events` o registro `empresa.apagada` (o id, o SHA-256 do nome, as contagens por tabela, quantos artefatos e o que saiu do cofre, só contagens, quem pediu e quando). Se o S3 ou o cofre falharem, nada do banco é apagado: repita o comando. Antes de apagar, o comando confere que a configuração alcança tudo o que a empresa tem fora do banco e, se não alcança, recusa sem apagar nada, dizendo o que falta: artefatos cifrados no S3 pedem `RAPA_ARMAZENAMENTO=s3` e `RAPA_S3_*`; chaves do transit, `RAPA_CHAVES=openbao`; credenciais registradas, `RAPA_COFRE=openbao` (com `RAPA_BAO_*` e o papel `rapa-operacao`). Se os segredos das credenciais não estão no OpenBao (o arquivo de desenvolvimento), diga com `--segredos-fora-do-openbao` (ou rode com `RAPA_COFRE=dev`): o registro `empresa.apagada` conta quantas credenciais ficaram com o segredo fora (`segredos_fora_do_openbao`). Depois, a pasta dos arquivos de entrada. A mensagem final diz quantos artefatos e quais pastas saíram (confira que são as do servidor). Se o comando parar depois de apagar as linhas (a pasta dos arquivos de entrada que não sai, por exemplo por permissão), corrija e rode o mesmo comando de novo, com o mesmo nome: ele confere o registro `empresa.apagada` e termina a pasta, os artefatos e as chaves (o que já saiu conta zero). Não tem volta: a exportação é a única cópia.

Para conferir depois:

```sql
SELECT at, action, actor, data FROM platform_events ORDER BY id DESC LIMIT 5;
```

### Resíduos

- Os comandos antigos (`empresa NOME`, `area` e `usuario`) continuam valendo, auditados (com `--por`), mas sem o vínculo com a organização.
- Uma organização que ainda não estava ligada fica "não ligada" no cache de cada processo por até 60 segundos.
- As travas do banco aceitam apagar só pelo dono das tabelas com a marca `rapa.purge_company`: quem tem a senha do dono continua podendo apagar à mão (como sempre pôde, desligando as travas).
- Sem o OpenBao, nenhuma chave por empresa existe para destruir (o comando só registra); as chaves dos webhooks derivadas do sal somem com as linhas.
- Uma exclusão que falha depois de o S3 ou o cofre terem apagado uma parte deixa o banco inteiro; a repetição apaga o resto, e o registro conta só o que sobrou.
- Uma empresa que fica congelada por muito tempo segura a remoção das partições de eventos em que tem eventos (as das outras empresas ficam junto, porque a partição é de todas).

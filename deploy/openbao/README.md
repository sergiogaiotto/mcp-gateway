# OpenBao do RaPA

O RaPA usa o OpenBao para duas coisas: os segredos das credenciais registradas (KV v2) e a
chave de cada empresa, que embrulha a chave de dados de cada execução com artefatos no S3
(transit). As regras estão em `docs/CONTRIBUINDO.md` (regras do cofre, do armazenamento e
das chaves).

## Arquivos

- `politicas/rapa-despachante.hcl`: lê `rapa/data/empresas/*`; embrulha e desembrulha com
  as chaves transit `empresa-*`. A chave de uma empresa nasce no primeiro embrulho: o
  `encrypt` do transit confere se a chave existe e, sem ela, o pedido é uma criação
  (`create`), que cria a chave com os padrões (`aes256-gcm96`, nunca exportável, sem cópia
  em claro). Nada em `rapa-transit/keys/`: ali não há essa conferência, todo POST é uma
  mudança (`update`), e `update` naquele caminho deixaria tornar a chave exportável, girar
  e cortar versões. Não ligue `disable_upsert` em `rapa-transit/config/keys`.
- `politicas/rapa-api.hcl`: só desembrulha (para servir um artefato cifrado); é a política
  dos papéis da interface (`rapa-web`) e da API (`rapa-api`). Nenhuma das duas lê segredos:
  as sessões de gravação não usam o cofre (quem demonstra digita no navegador da sessão).
- `politicas/rapa-operacao.hcl`: desembrulha (exportar), lista e apaga os metadados de
  `rapa/metadata/empresas/*`, lê e apaga as chaves transit (`rapa-transit/keys/+`, um
  segmento só) e, na configuração delas (`rapa-transit/keys/+/config`), só libera a
  exclusão (`allowed_parameters`: `deletion_allowed` = `"true"`); nunca cria, gira, corta
  ou exporta uma chave. Como o `+` não restringe o nome, a montagem do transit é só do
  RaPA (nenhuma outra chave nela).
- `custodiante.sh <uuid da empresa>`: a política `rapa-custodiante-<uuid>` de quem grava os
  segredos das credenciais de uma empresa (seção "Custodiantes").
- `preparar.sh`: monta o KV v2 (`rapa`) e o transit (`rapa-transit`), liga o método
  kubernetes e escreve as três políticas e os quatro papéis (um por conta de serviço: o
  despachante, a interface, a API e a operação). Idempotente e sem segredo no arquivo: quem
  administra o cofre entra antes (`bao login`) e roda o script.

## Preparar

```bash
export BAO_ADDR=https://openbao.cofre.svc:8200 BAO_CACERT=/caminho/do/ca.pem
bao login -method=oidc
RAPA_NAMESPACE=rapa ./deploy/openbao/preparar.sh
```

O script roda de qualquer máquina com o CLI `bao` (no job kind do CI, do runner, pelo
port-forward: a imagem do OpenBao não tem o bash). Com o OpenBao num pod do cluster, ele
confere os tokens das contas de serviço com o CA e o token montados no pod dele; só com o
OpenBao fora do cluster informe o CA da API do Kubernetes em `KUBERNETES_CA_CERT` (um
arquivo PEM) e, obrigatório junto com ele, `KUBERNETES_TOKEN_REVIEWER_JWT`: um arquivo com o
token de uma conta de serviço do cluster com o papel `system:auth-delegator` (só para a
TokenReview; o token projetado dos componentes tem a audiência `openbao`, que a API do
Kubernetes recusa como credencial, e as contas do RaPA não têm esse papel). O script
reescreve o config do método inteiro: informe o arquivo a cada execução (sem ele, o script
para antes de mexer), e troque-o antes de o token vencer. As contas de serviço padrão (`RAPA_SA_DESPACHANTE`, `RAPA_SA_WEB`,
`RAPA_SA_API` e `RAPA_SA_OPERACAO`) são as do chart; troque só se trocar `nameBase` nele.

Os segredos de uma empresa ficam em `rapa/data/empresas/<uuid da empresa>/<vault_path>`, com
os campos `username`, `password`, `totp_secret`, `pfx_base64` (o certificado A1 em base64) e
`pfx_password`. O `vault_path` é o que foi registrado na credencial; o prefixo da empresa é
sempre posto pelo RaPA. Exemplo, com um valor fictício:

```bash
bao kv put -mount=rapa empresas/00000000-0000-4000-8000-000000000001/portal/acesso \
  username=usuario-de-exemplo password=@/caminho/da/senha-de-exemplo.txt
```

## Custodiantes

Quem grava os segredos de uma empresa é o custodiante dela, direto no OpenBao (o RaPA nunca
recebe o valor). Cada empresa tem a própria política, só debaixo do prefixo dela:

```bash
./deploy/openbao/custodiante.sh 00000000-0000-4000-8000-000000000001
```

A política `rapa-custodiante-<uuid>` grava (`create`, `update`) em
`rapa/data/empresas/<uuid>/*` e lista `rapa/metadata/empresas/<uuid>/*`; não lê o valor de um
segredo, não apaga e não alcança outra empresa. Ligue-a só à identidade do custodiante daquela
empresa no método de entrada do cofre (um grupo do OIDC do Keycloak, um usuário do
`userpass`), criado com o token raiz antes de revogá-lo, ou depois por quem administra o cofre
(docs/OPERACAO.md 1.6). Nunca dê a um custodiante uma política de `empresas/*`: ela deixaria o
de uma empresa trocar os segredos de outra (e o despachante os entregaria às execuções dela).

## No cluster

Cada componente entra com o token projetado da própria conta de serviço, com a audiência
`openbao` (`RAPA_BAO_TOKEN_SA`, padrão `/var/run/secrets/rapa/bao/token`):

| Componente | Conta de serviço | `RAPA_BAO_PAPEL` (`vault.roles` do chart) | Política |
|---|---|---|---|
| despachante | `rapa-despachante` | `rapa-despachante` | `rapa-despachante` |
| interface | `rapa-web` | `rapa-web` | `rapa-api` |
| API | `rapa-api` | `rapa-api` | `rapa-api` |
| operação (o pod `rapa-operacao` de `rapa.control.admin`) | `rapa-operacao` | `rapa-operacao` | `rapa-operacao` |

Cada papel só aceita a própria conta de serviço (no namespace `RAPA_NAMESPACE`): o token da
web não entra como despachante, e o da API não entra como operação. A web e a api só usam o
OpenBao com `RAPA_CHAVES=openbao` (para desembrulhar); o cofre dos segredos
(`RAPA_COFRE=openbao`) é só do despachante e da operação.

A conta de serviço do próprio OpenBao (fora do cluster, a do `KUBERNETES_TOKEN_REVIEWER_JWT`)
precisa do papel `system:auth-delegator` para conferir os tokens. O endereço (`RAPA_BAO_ENDERECO`) é o do serviço ativo: o RaPA não segue
redirecionamentos.

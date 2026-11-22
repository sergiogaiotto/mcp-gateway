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
- `politicas/rapa-api.hcl`: só desembrulha (para servir um artefato cifrado).
- `politicas/rapa-operacao.hcl`: desembrulha (exportar), lista e apaga os metadados de
  `rapa/metadata/empresas/*`, lê e apaga as chaves transit (`rapa-transit/keys/+`, um
  segmento só) e, na configuração delas (`rapa-transit/keys/+/config`), só libera a
  exclusão (`allowed_parameters`: `deletion_allowed` = `"true"`); nunca cria, gira, corta
  ou exporta uma chave. Como o `+` não restringe o nome, a montagem do transit é só do
  RaPA (nenhuma outra chave nela).
- `preparar.sh`: monta o KV v2 (`rapa`) e o transit (`rapa-transit`), liga o método
  kubernetes e escreve as três políticas e os três papéis. Idempotente e sem segredo no
  arquivo: quem administra o cofre entra antes (`bao login`) e roda o script.

## Preparar

```bash
export BAO_ADDR=https://openbao.cofre.svc:8200 BAO_CACERT=/caminho/do/ca.pem
bao login -method=oidc
RAPA_NAMESPACE=rapa ./deploy/openbao/preparar.sh
```

Os segredos de uma empresa ficam em `rapa/data/empresas/<uuid da empresa>/<vault_path>`, com
os campos `username`, `password`, `totp_secret`, `pfx_base64` (o certificado A1 em base64) e
`pfx_password`. O `vault_path` é o que foi registrado na credencial; o prefixo da empresa é
sempre posto pelo RaPA. Exemplo, com um valor fictício:

```bash
bao kv put -mount=rapa empresas/00000000-0000-4000-8000-000000000001/portal/acesso \
  username=usuario-de-exemplo password=@/caminho/da/senha-de-exemplo.txt
```

## No cluster

Cada componente entra com o token projetado da própria conta de serviço, com a audiência
`openbao` (`RAPA_BAO_TOKEN_SA`, padrão `/var/run/secrets/rapa/bao/token`):

| Componente | Conta de serviço | `RAPA_BAO_PAPEL` |
|---|---|---|
| despachante | `rapa-despachante` | `rapa-despachante` |
| interface e API | `rapa-web`, `rapa-api` | `rapa-api` |
| operação (Jobs de `rapa.control.admin`) | `rapa-operacao` | `rapa-operacao` |

A conta de serviço do próprio OpenBao precisa do papel `system:auth-delegator` para conferir
os tokens. O endereço (`RAPA_BAO_ENDERECO`) é o do serviço ativo: o RaPA não segue
redirecionamentos.

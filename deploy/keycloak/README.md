# Keycloak para o RaPA

O RaPA confia no Keycloak 26 (instalado pelo chart ou operador dele, no namespace `identidade`) para duas coisas: o login da interface (`rapa-web`) e os tokens da API REST e do MCP (`rapa-api`). A empresa de cada pessoa vem da claim `organization` (as Organizations do Keycloak 26), nunca de algo que a pessoa escolhe; os papéis vêm do banco do RaPA.

`realm-rapa.json` é um realm de exemplo sem nenhum segredo: o segredo do cliente da interface é gerado pelo Keycloak na importação, e nenhuma pessoa nem organização vem no arquivo.

## 1. Importar o realm

Troque `rapa.exemplo.test` pelo endereço público do RaPA (o mesmo de `publicUrl` no chart) nos `redirectUris`, `webOrigins` e `post.logout.redirect.uris` e importe:

- pela tela de administração: **Create realm** → **Resource file** → `realm-rapa.json`; ou
- pelo operador do Keycloak, com um `KeycloakRealmImport` que aponta para o conteúdo do arquivo; ou
- pela linha de comando do Keycloak: `kc.sh import --file realm-rapa.json`.

O que vem no realm:

- `organizationsEnabled: true`: as Organizations do Keycloak 26.
- `rapa-interface`: cliente OIDC **confidencial** da interface, só com o fluxo de código e PKCE `S256` (nada de fluxo implícito nem de senha direta). O retorno é `https://<endereço>/auth/oidc/retorno`.
- `rapa-api`: só a **audiência** dos tokens da API (`RAPA_JWT_AUDIENCE=rapa-api`). O cliente da interface põe essa audiência no token de acesso (mapper `audiencia-da-api`): o mesmo token vale na interface e na REST.
- O escopo `organization` entre os escopos padrão do `rapa-interface`: a claim `organization`, que o RaPA confere (`rapa.control.auth`): exatamente uma organização por pessoa, ligada a uma empresa. O mapper de organização do Keycloak 26 só põe a claim quando o escopo `organization` está no pedido (ou quando o login escolheu uma organização); como escopo padrão, ele vai em todo pedido, sem o RaPA pedi-lo. O escopo e o mapper dele são os que o Keycloak cria na importação (o arquivo não declara `clientScopes`); o cliente não tem um mapper próprio da mesma claim (dois mappers da mesma claim correm, numa ordem qualquer).

Depois de importar, ligue o id na claim: **Client scopes** → `organization` → **Mappers** → `organization` → **Add organization id** = On (e **Save**). Assim a claim vem com o apelido e o id (`{"apelido": {"id": "<uuid>"}}`), a forma que o RaPA liga à empresa pelo id da organização (`empresa criar --organizacao <id>`). Sem isso, a claim é só a lista dos apelidos (`["apelido"]`), e só uma empresa ligada pelo apelido aceita o login.

## 2. O segredo do cliente da interface

Em **Clients** → `rapa-interface` → **Credentials**, copie o segredo para um arquivo temporário com permissão 0600 (nunca na linha de comando, que fica no histórico) e crie o Secret que o chart referencia:

```bash
umask 077
read -rs segredo && printf '%s' "$segredo" > /tmp/segredo-oidc && unset segredo   # cole e Enter
kubectl -n rapa create secret generic rapa-oidc --from-file=client-secret=/tmp/segredo-oidc
shred -u /tmp/segredo-oidc
```

Nos valores do chart (só nomes, nenhum segredo):

```yaml
publicUrl: https://rapa.exemplo.test
auth:
  jwt:
    issuer: https://sso.exemplo.test/realms/rapa
    audience: rapa-api
    jwksUrl: https://sso.exemplo.test/realms/rapa/protocol/openid-connect/certs
  oidc:
    clientId: rapa-interface
    redirectUri: https://rapa.exemplo.test/auth/oidc/retorno
    existingSecret: rapa-oidc
```

E a saída da `rapa-web` e da `rapa-api` para o Keycloak em `network.egress.keycloak` (o namespace `identidade`, nas portas do Keycloak).

### O emissor dentro do cluster

O emissor (`auth.jwt.issuer`) é um nome público, o mesmo que o navegador usa no login, e a web também o chama de dentro do cluster: `<emissor>/.well-known/openid-configuration`, a troca do código e o JWKS. Se esse nome aponta para o balanceador ou para o Envoy externo, como de costume, o destino depois do DNAT é um pod de `envoy-gateway-system`, não do namespace `identidade`, e a NetworkPolicy recusa: o login e a conferência dos tokens falham. Dentro do cluster, o nome do emissor precisa chegar direto ao Service do Keycloak, com o mesmo endereço (o `iss` dos tokens não muda):

1. No CoreDNS (`kube-system/coredns`, no bloco `.:53` do `Corefile`), uma reescrita do nome público para o Service:

   ```
   rewrite name exact sso.exemplo.test keycloak.identidade.svc.cluster.local
   ```

   (No EKS, pelo complemento do CoreDNS, na configuração dele; `kubectl -n kube-system rollout restart deployment coredns` depois.)
2. O certificado do Keycloak (o do Service, na porta 8443) com o nome público entre os nomes dele, e o CA desse certificado na cadeia que os pods do RaPA conferem.
3. A porta: o Service do Keycloak na mesma porta do endereço público (443), ou o emissor com a porta do Service; `network.egress.keycloak.ports` já tem a 8443 e a 443.

Sem a reescrita, a alternativa é abrir a saída da web e da api para os pods do Envoy (`network.egress.keycloak.to` com o namespace `envoy-gateway-system`), sabendo que assim elas alcançam tudo o que o gateway publica (inclusive elas mesmas). Confira de dentro de um pod da web: `kubectl -n rapa exec deploy/rapa-web -- python -c "import urllib.request; print(urllib.request.urlopen('https://sso.exemplo.test/realms/rapa/.well-known/openid-configuration').status)"` precisa responder 200.

## 3. Uma organização por empresa

Para cada empresa cliente:

1. Em **Organizations** → **Create organization**: o nome, o apelido (por exemplo `empresa-exemplo`) e o domínio dos e-mails. Copie o **id** da organização (um UUID).
2. Em **Members**, convide ou adicione as pessoas da empresa. Uma pessoa pertence a uma organização só (com duas, o RaPA recusa o login).
3. Copie o **ID** (o `sub`) de quem vai administrar a empresa, em **Users** → a pessoa.
4. Crie a empresa no RaPA já ligada à organização, pelo CLI da operação por empresa, no pod da operação (docs/OPERACAO.md, 1.10):

```bash
kubectl -n rapa exec rapa-operacao -- python -m rapa.control.admin empresa criar "Empresa Exemplo" \
  --organizacao ID_DA_ORGANIZACAO --admin SUB_DO_ADMIN --nome-admin "Nome da pessoa" \
  --area "Fiscal" --vagas 2 --fila 1000 --por "$USER"
```

Prefira o id da organização (o apelido também é aceito, mas pode mudar). A ligação vale em até 60 segundos, sem reiniciar nada. Quem entra pela primeira vez vira usuário sem papel; o administrador da empresa dá os papéis na tela **Empresa**.

## 4. Conferir

Em **Clients** → `rapa-interface` → **Client scopes** → **Evaluate**, escolha uma pessoa da organização e veja o token de acesso gerado: precisa ter `aud` com `rapa-api`, `organization` no `scope` e a claim `organization` com o apelido e o id (sem a claim, o RaPA recusa o login com "o token não informa a empresa do usuário"). Depois, na interface, a pessoa entra e vê a empresa dela; pela REST, `GET /v1/eu` com o token mostra a empresa e os papéis.

## Outros clientes da API (agentes MCP)

Um cliente que chama a REST ou o MCP com o próprio token (um agente, outro sistema) precisa do mesmo mapper `audiencia-da-api`, do escopo `organization` entre os escopos padrão dele (ou pedido no `scope` de cada token) e de pertencer a uma organização (ou à conta de serviço de uma). Nunca dê a ele o fluxo de senha direta.

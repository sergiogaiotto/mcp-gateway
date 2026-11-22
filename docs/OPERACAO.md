# Operação das empresas

Roteiro curto para o time da plataforma: a entrada de uma empresa, congelar, exportar e apagar. Os comandos são de `python -m rapa.control.admin`. As regras completas estão nas docstrings de `rapa.control.services.lifecycle`, `rapa.control.services.company` e da migração `v0007_operacao_empresas`.

Duas URLs de banco:

- a do papel da aplicação (`rapa_app`), em `--url` ou `RAPA_DATABASE_URL`, para a entrada e o consumo;
- a do dono das tabelas (`rapa_owner`), em `--url-dono` ou `RAPA_OWNER_DATABASE_URL`, para congelar, descongelar, exportar e apagar. Com a URL da aplicação no lugar, o comando recusa com uma mensagem clara.

`--por` diz quem pediu e vai para a auditoria (padrão: o usuário do sistema). Nenhum comando imprime segredos.

## 1. Entrada

1. Crie a Organização da empresa no Keycloak e anote o id dela (ou o apelido).
2. Crie a empresa, com quem vai administrá-la e as primeiras áreas, numa transação só:

```bash
uv run python -m rapa.control.admin empresa criar "Nome da empresa" --organizacao ID_DA_ORGANIZACAO --admin SUB_DO_ADMIN --nome-admin "Nome da pessoa" --area "Fiscal" --area "Compras" --vagas 2 --fila 1000 --emprestimo 2.0
```

O comando imprime o id da empresa. A organização fica ligada à empresa no banco (`companies.idp_organization`): o serviço passa a aceitar os tokens dela em até 60 segundos, sem reiniciar. O mapa estático `RAPA_ORGANIZATION_ALIASES` continua valendo; se ele e o banco discordarem sobre uma organização, o login é recusado.

O formato diz se `--organizacao` é o id ou o apelido: um valor no formato de UUID é sempre o id da organização (guardado em minúsculas, como o Keycloak o põe no token) e é comparado só com o id do token; qualquer outro valor é o apelido e é comparado só com o apelido. Prefira o id. Uma organização cujo apelido tem o formato de UUID só pode ser ligada pelo id.

3. Quem entra pela primeira vez com um token dessa organização vira usuário sem papel nenhum e só vê o pedido para um administrador dar um papel. O administrador da empresa dá os papéis na tela **Empresa** (ou em `PUT /v1/empresa/usuarios/{id}/papeis`).

## 2. Consumo do mês

```bash
uv run python -m rapa.control.admin empresa consumo ID_DA_EMPRESA --mes 2026-11
```

`--json` imprime o mesmo relatório da API (`GET /v1/empresa/consumo?mes=2026-11`). O mês é o do fuso `America/Sao_Paulo`, pela data em que cada execução foi pedida.

## 3. Saída

A saída é sempre nesta ordem: congelar, exportar, entregar o arquivo, apagar.

### Congelar

```bash
uv run python -m rapa.control.admin empresa congelar ID_DA_EMPRESA --motivo "contrato encerrado"
```

Daí em diante a empresa lê tudo, mas nada novo começa (a API responde 423, `empresa_congelada`): nem execução (REST, MCP, interface, agenda, webhook, lote), nem versão, publicação (também a aprovação de um robô com certificado A1), agenda, webhook, lote, sessão de gravação ou conserto, nem registrar, renovar ou autorizar um certificado. O banco também recusa a linha nova nessas tabelas, mesmo de um caminho que não passe pelos serviços. Revogar uma credencial ou uma autorização do custodiante continua valendo (só tira acesso), e as evidências continuam abertas para quem vê a execução. As execuções que estavam na fila são canceladas (código `empresa_congelada`); as que estavam rodando terminam normalmente (o despachante nem pega o que sobrar na fila: a conferência do certificado A1 nem chega a rodar). As agendas registram cada ocorrência como pulada (`empresa_congelada`). Enquanto estiver congelada, nada da empresa expira na retenção (telas, vídeos, arquivos baixados, retratos, chaves de idempotência e a partição de eventos em que ela tem eventos) e ela não recebe aviso de vencimento do certificado: tudo fica como estava para a exportação. Para voltar:

```bash
uv run python -m rapa.control.admin empresa descongelar ID_DA_EMPRESA
```

### Exportar

```bash
uv run python -m rapa.control.admin empresa exportar ID_DA_EMPRESA --saida empresa.tar.gz
```

O arquivo nasce com permissão 0600 e nunca sobrescreve outro. Links simbólicos nas pastas da empresa nunca são seguidos (nem um arquivo trocado por um link no meio da exportação), e o que entra no arquivo é exatamente o que o manifesto confere. Dentro dele: `manifesto.json` (o SHA-256 e o número de linhas de cada arquivo), `tabelas/<tabela>.jsonl` (uma linha por registro, de toda tabela com `company_id`, mais a da empresa: também as credenciais, as autorizações do custodiante, as aprovações e os avisos de vencimento do A1, e as execuções com as marcas de artefatos expirados) e os arquivos da empresa em `arquivos/` e `artefatos/` (de `RAPA_PASTA_ARQUIVOS/<empresa>` e `RAPA_PASTA_ARTEFATOS/<empresa>`, ou de `--pasta-arquivos` e `--pasta-artefatos`: as telas, os vídeos e os arquivos baixados que a retenção ainda não apagou). Material de chave nunca sai: o sal dos webhooks vem como `null`; de uma credencial sai só a referência ao cofre e os metadados (o valor nunca passa pelo RaPA). Confira o manifesto antes de entregar:

```bash
tar -xzf empresa.tar.gz && sha256sum tabelas/*.jsonl
```

### Apagar

Só uma empresa congelada, sem execuções rodando nem sessões de gravação abertas, e com o nome exato:

```bash
uv run python -m rapa.control.admin empresa apagar ID_DA_EMPRESA --confirmar "Nome da empresa"
```

Antes de apagar, o comando grava em `platform_events` o registro `empresa.apagada` (o id, o SHA-256 do nome, as contagens por tabela, quem pediu e quando). Depois apaga todas as linhas da empresa, das tabelas filhas para as mães (inclusive as do certificado A1 e as das evidências), e por fim a empresa, numa transação só; em seguida os arquivos e os artefatos dela, e as chaves próprias (o `KeyDestroyer`; sem o cofre, só registra). Não tem volta: a exportação é a única cópia.

Para conferir depois:

```sql
SELECT at, action, actor, data FROM platform_events ORDER BY id DESC LIMIT 5;
```

## Resíduos

- Os comandos antigos (`empresa NOME`, `area` e `usuario`) continuam valendo, auditados (com `--por`), mas sem o vínculo com a organização.
- Uma organização que ainda não estava ligada fica "não ligada" no cache de cada processo por até 60 segundos.
- As travas do banco aceitam apagar só pelo dono das tabelas com a marca `rapa.purge_company`: quem tem a senha do dono continua podendo apagar à mão (como sempre pôde, desligando as travas).
- Sem o cofre (parte E), nenhuma chave por empresa existe para destruir; as chaves dos webhooks derivadas do sal somem com as linhas.
- A exportação e a exclusão leem e apagam as pastas locais (`RAPA_PASTA_ARQUIVOS` e `RAPA_PASTA_ARTEFATOS`), a disposição do store local de artefatos; com o store de objetos da parte E, elas passam a usar o store.
- Uma empresa que fica congelada por muito tempo segura a remoção das partições de eventos em que tem eventos (as das outras empresas ficam junto, porque a partição é de todas).

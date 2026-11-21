# RaPA

O RaPA cria robôs de automação web (RPA) sob demanda para pessoas de negócio, sem que ninguém precise ler código. A pessoa demonstra a tarefa uma vez num navegador na nuvem; o RaPA transforma a gravação num robô determinístico e o publica como tool MCP e como endpoint REST.

Tudo é código aberto. O modelo de linguagem (um modelo aberto num hub interno) só ajuda a criar e a consertar robôs; as execuções normais não usam LLM.

O plano completo está em [docs/PLANO-RaPA.md](docs/PLANO-RaPA.md).

## Situação

Fase 0, que não depende do AWS Outposts:

- [x] Formato do robô (`rapa.core.robot`): lista fechada de passos, validada, com efeito por passo (leitura, gravação, irreversível)
- [x] Templates sem lógica (`rapa.core.templates`) e portão de política determinístico (`rapa.core.policy`)
- [x] Contratos entre os módulos (`rapa.core.contracts`, `rapa.core.events`)
- [x] Portais de teste em pt-BR (`rapa.fixtures`) com login TOTP, tabela paginada, downloads, formulário com CPF, passo irreversível e uma variante que muda o layout
- [x] Executor sobre Playwright (`rapa.runner`), sem LLM, com confirmação do plano de controle antes de cada passo que grava (e do aviso de cada passo irreversível)
- [x] Gravador da demonstração e compilador determinístico (`rapa.recorder`, `rapa.compiler`)
- [x] Cliente do hub de LLM, sonda de capacidades e generalização assistida (`rapa.llm`)
- [x] Plano de controle: PostgreSQL multiempresa com isolamento por linha, fila, despachante e lançador local (`rapa.control`); uma execução que perde o executor só volta para a fila se nenhum passo que grava tinha começado (depois de um passo que grava, falha; depois de um irreversível, fica indeterminada)
- [x] API REST e gateway MCP com o SDK oficial (`rapa.control.api`, `rapa.control.mcp`)
- [x] Interface em NiceGUI (`rapa.ui`)
- [x] Montagem num serviço só (`python -m rapa.server`), cadastro pela linha de comando (`python -m rapa.control.admin`) e teste ponta a ponta: publicar pelo REST, chamar pelo MCP e pelo REST, executar no portal de teste
- [x] Demonstrar e Generalizar pela interface (`rapa.session`, `rapa.control.sessions`, tela Gravar): a pessoa grava a tarefa num navegador na nuvem que vê ao vivo (imagem, mouse e teclado com acentos, por um websocket com bilhete de uso único), para, revisa as propostas (entradas, nomes dos passos e repetições, das regras e, com o hub de pé, do modelo) e cria o rascunho do robô; sessões e gravações em tabelas próprias com isolamento por linha, limite por empresa e na reserva interativa, e auditoria

## Estrutura

```
src/rapa/
  core/       formato do robô, templates, política, edições, contratos e eventos
  runner/     executor sobre Playwright (python -m rapa.runner.worker)
  recorder/   gravador da demonstração
  session/    hospedeiro da sessão de gravação ao vivo (python -m rapa.session.host)
  compiler/   compilação da gravação em robô
  llm/        cliente do hub de LLM, sonda (python -m rapa.llm.probe) e generalização
  control/    banco, fila, despachante, lançador, API REST e gateway MCP
  ui/         interface em NiceGUI (python -m rapa.ui para o modo de desenvolvimento)
  server.py   o serviço num processo só: REST, MCP, interface e despachante
  fixtures/   portais de teste e robôs de exemplo
tests/        testes automatizados (pytest)
docs/         plano e documentação
```

## Desenvolvimento

Requisitos: Python 3.14, [uv](https://docs.astral.sh/uv/) e um Chromium. Em produção o executor usa o Chromium do Debian 13.

```bash
uv sync --all-extras
```

```bash
uv run pytest
```

```bash
uv run ruff check src tests && uv run mypy
```

Os testes de banco usam `RAPA_TEST_DATABASE_URL` (superusuário) ou sobem um PostgreSQL temporário com os binários locais (`initdb`, `pg_ctl`). Os testes com navegador procuram o Chromium em `RAPA_CHROMIUM_PATH`, `/usr/bin/chromium` ou `/opt/pw-browsers/chromium`. Os portais de teste respondem em `*.rapa.test`, que o Chromium resolve para 127.0.0.1.

## Rodando o RaPA localmente

O exemplo usa um PostgreSQL local e o modo de desenvolvimento (tokens fixos e login sem senha). Em produção, os tokens vêm do Keycloak e os segredos do OpenBao.

1. Crie os três papéis e o banco, como superusuário do PostgreSQL: `rapa_owner` é o dono das tabelas, `rapa_app` serve a aplicação (sujeito ao isolamento por empresa) e `rapa_dispatcher` serve o despachante.

```sql
CREATE ROLE rapa_owner LOGIN PASSWORD 'SENHA';
CREATE ROLE rapa_app LOGIN PASSWORD 'SENHA' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
CREATE ROLE rapa_dispatcher LOGIN PASSWORD 'SENHA' NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE;
CREATE DATABASE rapa OWNER rapa_owner;
```

2. Crie as tabelas como dono:

```bash
uv run python -m rapa.control.db.migrate --url postgresql://rapa_owner:SENHA@localhost/rapa
```

3. Cadastre uma empresa, uma área e as pessoas. Cada comando imprime o identificador criado.

```bash
export RAPA_DATABASE_URL=postgresql://rapa_app:SENHA@localhost/rapa
```

```bash
uv run python -m rapa.control.admin empresa "Minha empresa" --vagas 2
```

```bash
uv run python -m rapa.control.admin area EMPRESA "Fiscal"
```

```bash
uv run python -m rapa.control.admin usuario EMPRESA ana@empresa --papel creator:AREA --papel operator
```

```bash
uv run python -m rapa.control.admin usuario EMPRESA bruno@empresa --papel approver:AREA
```

4. Escreva `tokens.json` com um token de desenvolvimento por pessoa, no formato `{"token-ana": {"sujeito": "ana@empresa", "empresa": "EMPRESA"}}`. Os segredos dos portais vão em `segredos.json`, no formato `{"EMPRESA": {"portal": {"username": "...", "password": "..."}}}`.

5. Suba o serviço:

```bash
RAPA_DEV=1 RAPA_COOKIE_SEGURO=0 RAPA_TOKENS_DEV=tokens.json RAPA_SEGREDOS_DEV=segredos.json RAPA_DISPATCHER_DATABASE_URL=postgresql://rapa_dispatcher:SENHA@localhost/rapa uv run python -m rapa.server --port 8000
```

A interface fica em http://127.0.0.1:8000, a API REST em `/v1` e o gateway MCP em `/mcp`, com `Authorization: Bearer token-ana`. Um robô criado por uma pessoa precisa ser publicado por outra, com o papel de aprovador.

Para gravar um robô, entre como quem tem o papel de criação e abra **Gravar**: informe a área e o endereço inicial, faça a tarefa na tela ao vivo e clique em **Parar e revisar**. Cada gravação abre um processo `python -m rapa.session.host` com um Chromium completo (`RAPA_CHROMIUM_PATH`, `RAPA_ARGS_NAVEGADOR`), cuja saída passa por uma guarda que recusa a própria máquina e o metadata da nuvem (as redes privadas do plano de controle ficam com a NetworkPolicy do deploy); os limites e prazos vêm de `RAPA_SESSOES_POR_EMPRESA`, `RAPA_SESSOES_MAX`, `RAPA_SESSAO_OCIOSA_S` e `RAPA_SESSAO_MAX_S`. Com `RAPA_PUBLIC_URL`, o websocket da visão ao vivo só aceita a origem da interface (mais as de `RAPA_ORIGENS_AO_VIVO`). As regras estão em [docs/CONTRIBUINDO.md](docs/CONTRIBUINDO.md). Com o hub de LLM configurado (`RAPA_LLM_BASE_URL`, `RAPA_LLM_MODEL`, `RAPA_LLM_API_KEY` e a matriz da sonda em `RAPA_LLM_CAPACIDADES`, gravada por `python -m rapa.llm.probe --out`), a revisão também mostra as propostas do modelo; sem ele, só as sugestões das regras, com um aviso.

Os arquivos de entrada (para robôs que enviam arquivos) ficam em `RAPA_PASTA_ARQUIVOS`, numa subpasta por empresa: `<RAPA_PASTA_ARQUIVOS>/<uuid da empresa>/<arquivo>`. Cada execução só enxerga a subpasta da própria empresa; arquivos soltos na raiz não valem. `/saude` responde 503 enquanto o despachante do processo estiver parado.

## Licença

Apache-2.0. Veja [LICENSE](LICENSE).

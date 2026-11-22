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
- [x] Publicar com agenda, webhook e lote por planilha (`rapa.core.cron`, `rapa.control.services.schedules`, `webhooks`, `batches` e `sheets`; telas Agendas, Webhooks e Lotes; `/v1/agendas`, `/v1/robos/{id}/webhooks`, `/v1/webhooks/{id}` e `/v1/lotes`): agendas cron num fuso (`America/Sao_Paulo` por padrão), disparadas pelo despachante uma vez por ocorrência, com tolerância para atrasos e as ocorrências ignoradas registradas; webhooks sem token, assinados com HMAC-SHA256 sobre o horário, o identificador da entrega e o corpo, com proteção contra repetição e limite por webhook, e a chave mostrada uma vez (derivada de `RAPA_CHAVE_WEBHOOKS`, nunca guardada); lotes de xlsx (sem fórmulas) ou CSV, conferidos linha a linha antes de criar qualquer coisa (tudo ou nada), com andamento e resultado em CSV ou xlsx; prioridades do plano (interativo, MCP e API, agenda e webhook, lote), seis tabelas novas com isolamento por linha (migração 0004) e auditoria
- [x] Consertar (`rapa.repair`, `rapa.llm.repair`, `rapa.control.services.repairs`, tela "Consertar este passo" e `/v1/execucoes/{id}/conserto`): quando o alvo de um passo não é encontrado (ou é encontrado mais de uma vez), o executor guarda um retrato só de leitura dos elementos da página, sem valores de campos nem segredos; o código enumera os candidatos para aquele passo, com os localizadores do compilador e uma nota determinística, sem executar nada de novo; o modelo só escolhe um deles (ou nenhum), validado por esquema, e sem o hub a pessoa escolhe sozinha; o conserto vira o rascunho da próxima versão, só com aquele alvo trocado, que outra pessoa, com o papel de aprovação, publica; desligado para robôs com certificado A1; duas tabelas novas com isolamento por linha (migração 0005) e auditoria
- [x] Regras do certificado A1 (`rapa.control.services.credentials` e `a1`, tela Credenciais, cartão do A1 na tela do robô, `/v1/credenciais` e `/v1/robos/{id}/autorizacoes-a1`): credenciais registradas só pela referência ao cofre, com custodiante, validade e impressão do certificado; a autorização do custodiante por robô e portal, que cai quando o certificado é trocado; duas aprovações de pessoas diferentes (nenhuma delas o custodiante, e nunca de uma versão que o custodiante criou) para publicar um robô com A1, também no banco; o certificado conferido ao enfileirar (todas as origens) e de novo pelo despachante antes de lançar (falha fechada, `certificado_a1_bloqueado`); avisos de vencimento uma vez por dia; três tabelas novas com isolamento por linha (migração 0006) e auditoria
- [x] Operação por empresa (`rapa.control.services.company`, `rapa.control.services.lifecycle`, tela Empresa, `/v1/empresa` e [docs/OPERACAO.md](docs/OPERACAO.md)): a empresa entra pela linha de comando já ligada à organização do Keycloak (sem reiniciar o serviço) e com o administrador; quem entra pela primeira vez vira usuário sem papel; a própria empresa administra usuários, papéis e áreas e vê o consumo do mês; a plataforma congela (nada novo começa, a fila é cancelada), exporta tudo (sem material de chave) e apaga a empresa, com o registro em `platform_events` (migração 0007)
- [x] Evidências (`rapa.control.artifacts`, `rapa.control.services.artifacts` e `retention`, `rapa.runner.video`; tela da execução e `/v1/execucoes/{id}/artefatos`): as telas, os arquivos baixados e, quando a execução falha ou fica indeterminada, o vídeo dela ficam guardados por execução e saem pela API e pela tela da execução para quem vê a execução, sem nenhum caminho do disco; uma saída de download é o nome do artefato. O despachante apaga, uma vez por hora, o que passou do prazo de retenção (prazos a validar com o jurídico)

## Estrutura

```
src/rapa/
  core/       formato do robô, templates, política, edições, contratos e eventos
  runner/     executor sobre Playwright (python -m rapa.runner.worker)
  recorder/   gravador da demonstração
  session/    hospedeiro da sessão de gravação ao vivo (python -m rapa.session.host)
  compiler/   compilação da gravação em robô
  repair/     candidatos do conserto a partir do retrato da página que quebrou
  llm/        cliente do hub de LLM, sonda (python -m rapa.llm.probe), generalização e escolha do conserto
  control/    banco, fila, despachante, agendas, webhooks, lotes, credenciais e regras do A1, evidências, lançador, API REST e gateway MCP
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

3. Cadastre uma empresa, uma área e as pessoas. Cada comando imprime o identificador criado. Com o Keycloak, use `empresa criar`, que já liga a empresa à Organização e cria quem a administra (veja [docs/OPERACAO.md](docs/OPERACAO.md)); quem entrar depois aparece sem papel, e o administrador dá os papéis na tela **Empresa**. No modo de desenvolvimento, sem o Keycloak, os comandos abaixo bastam.

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
RAPA_DEV=1 RAPA_COOKIE_SEGURO=0 RAPA_TOKENS_DEV=tokens.json RAPA_SEGREDOS_DEV=segredos.json RAPA_CHAVE_WEBHOOKS=CHAVE_DOS_WEBHOOKS RAPA_DISPATCHER_DATABASE_URL=postgresql://rapa_dispatcher:SENHA@localhost/rapa uv run python -m rapa.server --port 8000
```

A interface fica em http://127.0.0.1:8000, a API REST em `/v1` e o gateway MCP em `/mcp`, com `Authorization: Bearer token-ana`. Um robô criado por uma pessoa precisa ser publicado por outra, com o papel de aprovador.

Para gravar um robô, entre como quem tem o papel de criação e abra **Gravar**: informe a área e o endereço inicial, faça a tarefa na tela ao vivo e clique em **Parar e revisar**. Cada gravação abre um processo `python -m rapa.session.host` com um Chromium completo (`RAPA_CHROMIUM_PATH`, `RAPA_ARGS_NAVEGADOR`), cuja saída passa por uma guarda que recusa a própria máquina e o metadata da nuvem (as redes privadas do plano de controle ficam com a NetworkPolicy do deploy); os limites e prazos vêm de `RAPA_SESSOES_POR_EMPRESA`, `RAPA_SESSOES_MAX`, `RAPA_SESSAO_OCIOSA_S` e `RAPA_SESSAO_MAX_S`. Com `RAPA_PUBLIC_URL`, o websocket da visão ao vivo só aceita a origem da interface (mais as de `RAPA_ORIGENS_AO_VIVO`). As regras estão em [docs/CONTRIBUINDO.md](docs/CONTRIBUINDO.md). Com o hub de LLM configurado (`RAPA_LLM_BASE_URL`, `RAPA_LLM_MODEL`, `RAPA_LLM_API_KEY` e a matriz da sonda em `RAPA_LLM_CAPACIDADES`, gravada por `python -m rapa.llm.probe --out`), a revisão também mostra as propostas do modelo; sem ele, só as sugestões das regras, com um aviso.

Um robô publicado também roda por **Agendas** (expressão cron e fuso; o despachante dispara as ocorrências vencidas a cada `RAPA_AGENDA_INTERVALO_S` segundos e ainda roda uma ocorrência atrasada até `RAPA_AGENDA_TOLERANCIA_S`; até `RAPA_AGENDAS_POR_EMPRESA` agendas ligadas por empresa), por **Webhooks** (outro sistema faz `POST /v1/webhooks/{id}` com as entradas em JSON e os cabeçalhos `X-RaPA-Timestamp`, `X-RaPA-Delivery` (sem ponto) e `X-RaPA-Signature`; as leituras de webhook antes da assinatura ficam em `RAPA_WEBHOOKS_POR_SEGUNDO` por processo; ligue com `RAPA_CHAVE_WEBHOOKS`, um texto aleatório de pelo menos 32 caracteres, gerado uma vez, por exemplo com `openssl rand -hex 32`, e guardado como segredo: trocá-lo invalida as chaves de todos os webhooks) e por **Lotes** (uma planilha .xlsx ou .csv, uma execução por linha; limites em `RAPA_LOTE_MAX_LINHAS` e `RAPA_LOTE_MAX_BYTES`). Só quem opera (ou administra) a área cria agendas, webhooks e lotes.

Quando uma execução falha porque o elemento de um passo não foi encontrado, a tela da execução mostra **Consertar este passo**: quem cria robôs na área escolhe o elemento novo entre os candidatos (com a sugestão do modelo, se o hub estiver configurado; `RAPA_CONSERTO_CANDIDATOS` diz quantos), e o conserto vira um rascunho da próxima versão, que outra pessoa, com o papel de aprovação, valida e publica na tela do robô.

Um robô com certificado digital A1 precisa de mais três coisas. Quem administra a área registra o certificado na tela **Credenciais** (só o caminho no cofre, o custodiante, a validade e a impressão SHA-256; nunca o arquivo nem a senha). O custodiante abre a tela do robô e clica em **Autorizar este robô neste portal**. Duas pessoas com o papel de aprovação, que não são o custodiante, aprovam a versão (a tela mostra "1 de 2 aprovações"). Quando o certificado é renovado no cofre, o custodiante registra a validade nova e, se o certificado tinha a impressão registrada, a impressão do certificado novo (com a impressão trocada, ele autoriza os robôs de novo). O aviso de vencimento aparece no topo da tela para custodiantes e administradores a partir de `RAPA_A1_AVISO_DIAS` dias antes (30 por padrão). Um certificado vencido, revogado ou sem a autorização não deixa o robô rodar, por nenhuma origem.

Quem administra a empresa abre **Empresa** para dar e tirar papéis (por área ou na empresa inteira), desativar e reativar pessoas e criar e renomear áreas; quem administra uma área faz o mesmo só com os papéis dela; quem administra a empresa ou audita vê ali o consumo do mês, por área e por origem (também em `GET /v1/empresa/consumo?mes=AAAA-MM` e em `python -m rapa.control.admin empresa consumo EMPRESA --mes AAAA-MM`). Para a saída de uma empresa (congelar, exportar e apagar), siga [docs/OPERACAO.md](docs/OPERACAO.md): esses comandos usam a URL do dono das tabelas (`RAPA_OWNER_DATABASE_URL`).

A tela de uma execução que terminou mostra as **Evidências**: as telas em miniatura (clique para ver grande), o vídeo da falha e os arquivos baixados, com tamanho e data. Pela API, `GET /v1/execucoes/{id}/artefatos` lista os artefatos e `GET /v1/execucoes/{id}/artefatos/{nome}` baixa um deles (a saída de um passo de download é esse nome). Quem vê a execução vê as evidências. O vídeo só é guardado quando a execução falha ou fica indeterminada, e só se o robô não digita credencial do cofre fora de um campo de senha; `RAPA_VIDEO_EM_FALHAS=0` desliga a gravação, que precisa do `ffmpeg` do Playwright no executor (`playwright install ffmpeg`; sem ele, as execuções rodam sem vídeo, e o servidor avisa no log ao subir). O despachante apaga o que venceu uma vez por hora: as telas e o vídeo de execuções que deram certo depois de `RAPA_RETENCAO_SUCESSO_DIAS` (7); as evidências (tudo de uma execução que falhou ou ficou indeterminada, e os arquivos baixados das que deram certo) e os retratos do conserto depois de `RAPA_RETENCAO_FALHAS_DIAS` (90); as chaves de idempotência vencidas; e os eventos depois de `RAPA_RETENCAO_EVENTOS_DIAS` (400). Esses prazos ainda estão a validar com o jurídico; o que foi apagado aparece como **expirado**.

Os arquivos de entrada (para robôs que enviam arquivos) ficam em `RAPA_PASTA_ARQUIVOS`, numa subpasta por empresa: `<RAPA_PASTA_ARQUIVOS>/<uuid da empresa>/<arquivo>`. Cada execução só enxerga a subpasta da própria empresa; arquivos soltos na raiz não valem. `/saude` responde 503 enquanto o despachante do processo estiver parado.

## Licença

Apache-2.0. Veja [LICENSE](LICENSE).

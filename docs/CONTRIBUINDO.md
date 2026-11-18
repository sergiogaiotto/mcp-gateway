# Como contribuir com o RaPA

## Regras do projeto

- **Só código aberto.** Toda dependência precisa de licença aprovada pela OSI. Bibliotecas com GPL ou AGPL não podem ser importadas no processo do RaPA; só rodam como serviço separado e sem modificação.
- **Sem LLM nas execuções.** O executor só interpreta a lista de passos. O modelo de linguagem entra apenas para criar e consertar robôs, e sempre como proponente: o código decide.
- **A empresa é fronteira obrigatória.** Toda tabela com dados de empresa tem `company_id` e isolamento por linha; nada que venha de um usuário escolhe a empresa, que sempre vem do token.
- **Segredos nunca aparecem** em eventos, logs, prompts, telas ou resultados. O executor digita o valor do cofre na hora; o resto do sistema só vê a referência.
- **Nada que gravou é repetido automaticamente.** Depois de um passo irreversível, uma falha vira estado "indeterminado" e uma pessoa decide.

## Organização do código

```
src/rapa/
  core/       formato do robô, templates, política, edições, contratos e eventos
  runner/     executor sobre Playwright
  recorder/   gravador da demonstração
  compiler/   compilação da gravação em robô
  llm/        cliente do hub de LLM, sonda e generalização
  control/    plano de controle: banco, fila, API REST e gateway MCP
  ui/         interface em NiceGUI
  fixtures/   portais de teste e robôs de exemplo
```

Os módulos conversam pelos contratos de `rapa.core.contracts` e `rapa.core.events`. Mudanças no formato do robô (`rapa.core.robot`) exigem subir `schema_version` e escrever a migração.

## Antes de abrir um PR

```bash
uv run ruff format src tests
```

```bash
uv run ruff check src tests && uv run mypy
```

```bash
uv run pytest
```

Marcadores do pytest: `browser` (abre o Chromium), `database` (precisa de PostgreSQL) e `slow` (testes longos, como 100 reexecuções).

Textos para usuários, mensagens de erro e documentação ficam em pt-BR.

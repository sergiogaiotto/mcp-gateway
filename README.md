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
- [ ] Executor sobre Playwright
- [ ] Gravador e compilador
- [ ] Cliente do hub de LLM e sonda de capacidades
- [ ] Plano de controle: Postgres multiempresa, fila, API REST e gateway MCP
- [ ] Interface em NiceGUI

## Estrutura

```
src/rapa/
  core/       formato do robô, templates, política, contratos e eventos
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

Os testes com navegador procuram o Chromium em `RAPA_CHROMIUM_PATH`, `/usr/bin/chromium` ou `/opt/pw-browsers/chromium`. Os portais de teste respondem em `*.rapa.test`, que o Chromium resolve para 127.0.0.1.

## Licença

Apache-2.0. Veja [LICENSE](LICENSE).

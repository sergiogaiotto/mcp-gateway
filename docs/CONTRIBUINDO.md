# Como contribuir com o RaPA

## Regras do projeto

- **Só código aberto.** Toda dependência precisa de licença aprovada pela OSI. Bibliotecas com GPL ou AGPL não podem ser importadas no processo do RaPA; só rodam como serviço separado e sem modificação.
- **Sem LLM nas execuções.** O executor só interpreta a lista de passos. O modelo de linguagem entra apenas para criar e consertar robôs, e sempre como proponente: o código decide.
- **A empresa é fronteira obrigatória.** Toda tabela com dados de empresa tem `company_id` e isolamento por linha; nada que venha de um usuário escolhe a empresa, que sempre vem do token.
- **Segredos nunca aparecem** em eventos, logs, prompts, telas ou resultados. O executor digita o valor do cofre na hora; o resto do sistema só vê a referência.
- **Nada que gravou é repetido automaticamente.** O executor só começa um passo que grava depois que o plano de controle registra o aviso; a partir daí a execução nunca volta para a fila: se parar, falha (e uma pessoa confere o sistema de destino). Depois de um passo irreversível, uma falha vira estado "indeterminado" e uma pessoa decide.

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
  server.py   montagem do serviço num processo só
tests/e2e/    o serviço inteiro sobre PostgreSQL, Chromium e o portal de teste
```

Os módulos conversam pelos contratos de `rapa.core.contracts` e `rapa.core.events`. Mudanças no formato do robô (`rapa.core.robot`) exigem subir `schema_version` e escrever a migração.

## Regras do gravador e do compilador

As regras completas estão nas docstrings de `rapa.recorder.sensitivity`, `rapa.recorder.recorder` e `rapa.compiler.lowering`, e no cabeçalho de `capture.js`. Na dúvida, o passo grava e o valor fica fora da gravação. Em resumo:

- **Login só com credenciais.** O envio só é autenticação quando a página só teve credenciais de uma credencial: o usuário com uma senha `type=password` de verdade (e, se houver, o código de uso único); usuário e código sem senha nunca é login. Só códigos só é login com o botão num `<form>` cujos campos de texto são só códigos, os mesmos que foram preenchidos; sem esse `<form>`, nunca, qualquer que seja o texto do botão.
- **Resíduo conhecido.** Um `<form>` só com o código continua login mesmo que a página use o código para confirmar uma operação. Como só os campos de texto contam, isso inclui o `<form>` do código com o valor num `<input type=hidden>` ou com uma lista que a pessoa não mexeu; dois `<form>` iguais só com o código na mesma página não se distinguem.
- **Shadow roots.** Um campo de shadow root aberto nunca é credencial de login e, numa página com senha ou código, sai da gravação e vira uma entrada do robô. Ele é identificado pelo caminho dos hospedeiros e localizado por um seletor que atravessa os shadow roots abertos (`#hospedeiro > #campo`), além do papel, do rótulo, do placeholder e do `data-testid`, contados no documento e em todos os shadow roots abertos. O valor de um campo de texto sai no `change` do próprio shadow root, antes do blur e na ordem da pessoa; o gravador não embrulha nenhum método nativo.
- **Um passo, um elemento.** Edições, marcações e escolhas só se juntam num passo quando são do mesmo campo (a mesma descrição, inclusive o `data-testid`) e do mesmo elemento (o número que o gravador dá a cada elemento): o alvo e o valor de um passo sempre vêm do mesmo elemento.
- **Limpeza sem caixa.** Os valores sigilosos saem de todo texto que pode chegar ao robô ou ao modelo de linguagem, em maiúsculas ou minúsculas; só os valores digitados, a chave da linha e os endereços são trocados pelo valor exato.

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

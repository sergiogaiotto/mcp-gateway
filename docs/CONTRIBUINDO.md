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
- **Shadow roots.** Um campo de shadow root aberto nunca é credencial de login e, numa página com senha ou código, o valor dele sai da gravação e o campo vira uma entrada do robô; só o `type=password` de um componente continua a senha do cofre, no campo dele. Ele é identificado pelo caminho dos hospedeiros e localizado por um seletor que atravessa os shadow roots abertos (`#hospedeiro > #campo`), além do papel, do rótulo, do placeholder e do `data-testid`, contados no documento e em todos os shadow roots abertos.
- **Quando sai o valor de um campo de texto de shadow root.** No `change` do próprio shadow root (o gravador põe ouvintes de `change`, `blur` e `focusout`, na fase de captura, em cada shadow root aberto que aparece no caminho de um evento), antes do blur e na ordem da pessoa. Se o componente para o `change`, o valor sai antes da próxima ação noutro elemento (`pointerdown`, `keydown` ou `beforeinput`) ou no `blur`, que o gravador ouve na fase de captura da janela e do shadow root, antes do ouvinte de blur do próprio campo (também quando a pessoa sai para um iframe). Se o componente já tirou o campo da página quando o `change`, o `blur` ou o `focusout` chega (ou quando a janela perde o foco), sai o descritor que o gravador guardou no primeiro foco ou input do campo, com o último valor que a pessoa digitou e a mesma marca de sigilo: um valor sigiloso só sai como segredo, para a limpeza.
- **Nada de shadow DOM é embrulhado.** `attachShadow`, `addEventListener`, `composedPath`, `focus` e `blur` continuam nativos. O único método nativo que o gravador troca é o setter de `value` de `HTMLInputElement` e `HTMLTextAreaElement`, que só marca o valor que a página muda enquanto a pessoa digita.
- **Resíduos dos shadow roots.** Shadow roots fechados ficam invisíveis. Um componente cujo ouvinte de captura, posto antes do do gravador, tira o campo da página (no próprio `change`, por exemplo) ainda perde o valor quando o gravador não tem o valor guardado (nenhum input confiável da pessoa no campo) ou não vê o campo sair: nenhum `change`, `blur` ou `focusout` dele chega ao gravador nem a janela perde o foco, como quando o componente também para o `change` e o foco muda por script no mesmo documento. Então o robô fica sem o passo, nunca com um valor errado.
- **Um passo, um elemento.** Edições, marcações e escolhas só se juntam num passo quando são do mesmo campo (a mesma descrição, inclusive o `data-testid`) e do mesmo elemento (o número que o gravador dá a cada elemento): o alvo e o valor de um passo sempre vêm do mesmo elemento. O segundo clique de um clique duplo só some no passo do primeiro quando é no mesmo elemento, ou quando um dos dois cliques não gravou nada: o robô nunca clica duas vezes onde a pessoa agiu uma vez.
- **Limpeza sem caixa.** Os valores sigilosos saem de todo texto que pode chegar ao robô ou ao modelo de linguagem, em maiúsculas ou minúsculas; só os valores digitados, a chave da linha e os endereços são trocados pelo valor exato.

## Regras do executor

As regras completas estão na docstring de `rapa.runner.interpreter`. Na dúvida, o passo grava e falha fechado antes de agir. Em resumo:

- **Rede contra ações repetidas.** Uma ação que grava numa linha (a página e o texto que a escolhe) conta em cada endereço por onde a página principal passa depois dela, até o fim da volta e, depois, até o fim da repetição, menos os que o próprio `navigate` do robô abre. Perdas de disponibilidade conhecidas, que falham fechado: escolher a página por um clique (um link) numa volta seguinte, e uma página aberta por um `navigate` que vai sozinha a outro endereço depois (`replaceState`, `meta refresh`, redirecionamento por script).
- **Confirmação.** A espera pela confirmação do anúncio de um passo que grava tem só o prazo da própria confirmação, nem o do passo nem o da execução: o lançador mata o processo do executor 15 s depois do prazo da execução, e o passo nunca age se esse prazo acabou durante a espera. Depois da confirmação do aviso de passo irreversível, o alvo, a linha única e a rede são conferidos de novo, na mesma página da primeira conferência; se a página for outra, o passo falha sem agir.
- **Agir no elemento conferido.** Um passo que grava age no elemento que foi conferido, nunca no que o localizador encontrar depois em outro documento. Se a página navega ou monta o elemento de novo entre a conferência e a ação, o passo falha sem agir (perda de disponibilidade aceita: uma linha montada de novo nesse meio tempo).
- **Texto conferido.** Depois de digitar um texto, o valor do campo é conferido. Se a página mudou o campo, o texto é digitado de novo, e vale se o campo ficar com ele ou exatamente com o que ficou da primeira vez (a máscara da própria página); senão, o passo falha. Num passo que grava, nada é digitado de novo: o campo com outro valor faz o passo falhar. Os valores do campo nunca vão para eventos nem mensagens. Uma mudança que a página faz depois, sozinha (num temporizador), não é vista.
- **Resíduos conhecidos.** A conferência da linha única não vê shadow roots fechados nem uma chave que só o CSS mostra, sem nada no HTML (o `content` de um `::before`).

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

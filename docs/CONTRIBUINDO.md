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
- **Quando sai o valor de um campo de texto de shadow root.** No `change` do próprio shadow root (o gravador põe ouvintes de `change`, `blur` e `focusout`, na fase de captura, em cada shadow root aberto que aparece no caminho de um evento), antes do blur e na ordem da pessoa, ou no `focusout`. Se o componente para o `change`, o valor sai antes da próxima ação noutro elemento (`pointerdown`, `keydown` ou `beforeinput`) ou no `blur`, que o gravador ouve na fase de captura da janela e do shadow root (também quando a pessoa sai para um iframe). Nesses quatro eventos o gravador só tira um retrato do campo (o descritor e o valor) e decide num `setTimeout`, depois dos ouvintes de blur do componente: o campo ainda na página sai com o descritor de agora (o que virou `type=password` ou código só sai como segredo); o que saiu da página sai com o retrato. Um clique e as teclas Enter, Tab e Esc mandam os valores pendentes na hora, antes de a ação ser gravada. Se o componente já tirou o campo da página quando o `change`, o `blur` ou o `focusout` chega (ou quando a janela perde o foco, só para campos de shadow root), sai o retrato que o gravador tirou depois da última tecla da pessoa, num `setTimeout`, com a tecla já aplicada e o campo ainda na página: o descritor e o valor são do mesmo momento. Na tecla (`beforeinput` ou `input`), o valor guardado fica desconhecido até o retrato seguinte.
- **Marcas de sigilo só sobem.** Senha (inclusive a máscara por CSS), código de uso único, papel de login (o usuário ou outro campo de um login) e nome que fala em senha: cada retrato do campo (no foco, depois de cada tecla, no `blur` e antes da próxima ação) soma as marcas dos anteriores até o valor sair, e o envio leva todas; o campo que saiu da página soma ainda as do próprio elemento e a de qualquer senha ou código no documento. Um valor sigiloso só sai como segredo, para a limpeza. Depois do envio, a próxima edição do campo começa outro retrato.
- **Nada de shadow DOM é embrulhado.** `attachShadow`, `addEventListener`, `composedPath`, `focus` e `blur` continuam nativos. O único nativo que o gravador troca é a propriedade `value` de `HTMLInputElement` e `HTMLTextAreaElement` (`hookValueSetter`, o getter e o setter): o getter só repassa, e o setter só marca o valor que a página muda enquanto a pessoa digita.
- **Resíduos dos shadow roots.** Shadow roots fechados ficam invisíveis. O campo que sai da página antes do retrato de depois da última tecla da pessoa (o componente o troca no próprio `input`, por exemplo) é esquecido, e também o que sai sem nenhum `change`, `blur` ou `focusout` que o gravador veja e sem a janela perder o foco (o componente para o `change` e o foco muda por script no mesmo documento); um campo da luz que some no próprio blur quando a pessoa sai para um iframe também. Então o robô fica sem o passo (falha fechada), nunca com um valor guardado antes da última tecla (uma tecla que a página só aplica depois, num temporizador dela, não é vista). O valor que sai antes do blur vai com o descritor desse momento: o campo que só vira senha no próprio blur ainda sai como texto quando o valor sai no `change` (que o navegador dispara antes do blur, quando o componente não o para) ou numa tecla Enter, Tab ou Esc no próprio campo.
- **Um passo, um elemento.** Edições, marcações e escolhas só se juntam num passo quando são do mesmo campo (a mesma descrição, inclusive o `data-testid`) e do mesmo elemento (o número que o gravador dá a cada elemento): o alvo e o valor de um passo sempre vêm do mesmo elemento.
- **Clique duplo.** O segundo clique (`detail` 2 ou mais, o mesmo seletor) some no passo do primeiro no mesmo elemento e também noutro elemento sem prova de outro alvo (outra chave da linha, outro texto, outro nome, outro rótulo ou outro `data-testid`): o número do elemento sozinho não separa os cliques. Com essa prova, ele só é outro passo quando cada um dos dois cliques gravou algo. Resíduos, em que o robô age menos que a pessoa, nunca mais: o clique duplo num botão sem trava que a página recria entre os cliques (cada clique grava) fica um passo só, e o botão de outra linha que sobe para o lugar do que a pessoa clicou, com o mesmo seletor cortado e nada mais diferente, também (o robô exclui um onde a pessoa excluiu dois).
- **Limpeza sem caixa.** Os valores sigilosos saem de todo texto que pode chegar ao robô ou ao modelo de linguagem, em maiúsculas ou minúsculas; só os valores digitados, a chave da linha e os endereços são trocados pelo valor exato.

## Regras do executor

As regras completas estão na docstring de `rapa.runner.interpreter`. Na dúvida, o passo grava e falha fechado antes de agir. Em resumo:

- **Rede contra ações repetidas.** Uma ação que grava numa linha (a página e o texto que a escolhe) conta em cada endereço por onde a página principal passa depois dela, até o fim da volta e, depois, até o fim da repetição, menos os que o próprio `navigate` do robô abre. Perdas de disponibilidade conhecidas, que falham fechado: escolher a página por um clique (um link) numa volta seguinte, e uma página aberta por um `navigate` que vai sozinha a outro endereço depois (`replaceState`, `meta refresh`, redirecionamento por script).
- **Confirmação.** A espera pela confirmação do anúncio de um passo que grava tem só o prazo da própria confirmação, nem o do passo nem o da execução: o lançador mata o processo do executor 15 s depois do prazo da execução, e o passo nunca age se esse prazo acabou durante a espera. Depois da confirmação do aviso de passo irreversível, o alvo, a linha única e a rede são conferidos de novo, na mesma página da primeira conferência; se a página for outra, o passo falha sem agir. Perda de disponibilidade aceita: uma página que sai e volta ao mesmo endereço durante essa espera também é recusada, porque a volta conta como da ação anterior na mesma linha.
- **Agir no elemento conferido, na hora de agir.** Um passo que grava age no elemento que foi conferido, nunca no que o localizador encontrar depois em outro documento. A espera pelo elemento (visível, habilitado e parado; editável para digitar; a opção na lista) vem antes, sem mandar nenhuma entrada à página. Depois dela, o contador de navegações da página principal (de outro documento e do mesmo documento: `pushState`, `replaceState`, `#`), a página e, numa linha escolhida pelo texto, a linha única com o elemento dentro dela são conferidos de novo, e a ação vai sem nova espera. Um vigia na fase de captura da janela confere o primeiro evento da ação: se ele não cai no alvo, se o endereço mudou ou se a linha perdeu a chave, a ação inteira é cancelada. Em qualquer desses casos o passo falha sem agir (`elemento_ambiguo`). Um botão montado de novo na mesma linha durante a espera, sem navegação, leva a uma nova conferência completa, uma vez; na segunda vez, ou se a página trocou de documento durante a espera, o passo falha sem agir (`acao_falhou`). Perdas de disponibilidade aceitas: qualquer mudança de endereço da página principal entre a conferência e a ação (também um filtro que muda a consulta do endereço), e uma tecla para um elemento que não recebe o foco.
- **Texto conferido.** Depois de digitar um texto, o valor do campo é conferido. Se a página mudou o campo, o texto é digitado de novo, e vale se o campo ficar com ele ou exatamente com o que ficou da primeira vez (a máscara da própria página); senão, o passo falha. Num passo que grava, nada é digitado de novo: vale o texto como o navegador o guarda (posto numa cópia solta do campo: `\r\n` vira `\n`, um e-mail perde os espaços das pontas), com os brancos e o espaço rígido normalizados num elemento editável, ou, num texto sem letras, com os mesmos dígitos na mesma ordem (máscaras de CPF, CNPJ, CEP, telefone, moeda e data); qualquer outra diferença, como o `maxlength` que cortou o texto ou uma página que troca letras, faz o passo falhar. Os valores do campo nunca vão para eventos nem mensagens.
- **Campos digitados, lidos de novo antes de gravar.** Os campos de texto digitados desde a última navegação da página principal (também `pushState`) são lidos de novo antes de cada passo que grava e age na página; se a página mudou algum depois da conferência (uma consulta de CEP que responde depois), o passo falha sem agir (`acao_falhou`). Vale o valor da conferência da digitação ou a mesma regra de comparação (uma máscara que a página só põe quando o foco sai do campo vale). Um campo digitado de novo vale pela última vez, e um que saiu da página deixa de contar. Perda de disponibilidade aceita: um campo digitado antes que a página limpa ou formata com letras depois de uma gravação, sem o robô digitar de novo, faz a próxima gravação no mesmo documento falhar.
- **Resíduos conhecidos.** A conferência da linha única não vê shadow roots fechados nem uma chave que só o CSS mostra, sem nada no HTML (o `content` de um `::before`); uma lista virtual que troca o que a linha é sem trocar o texto dela também não é vista. Um ouvinte da própria página na janela, posto antes do vigia, vê o evento antes dele. Um segredo vai para o elemento em que a origem foi conferida, sem o vigia.

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

"""Interpretador determinístico dos robôs do RaPA, sobre Playwright e sem LLM.

O interpretador percorre a lista de passos do manifesto, num contexto de
navegador novo, e devolve um :class:`~rapa.runner.result.RunResult`. Regras
principais:

- um localizador só serve se encontrar **exatamente um** elemento; passos de
  leitura podem usar localizadores alternativos (evento ``step_healed``), mas
  passos que gravam só usam o principal;
- todo endereço aberto precisa estar nos domínios permitidos do robô, e
  segredos só são digitados na origem declarada da credencial;
- todo passo simples que grava se anuncia antes de agir (evento
  ``step_started`` com ``grava: true``), e antes de um passo irreversível agir o
  executor avisa (``irreversivel: true``); com confirmação, o executor só segue
  quando o plano de controle confirma que gravou o anúncio, e a espera por ela
  nunca conta no prazo do passo (que só corre depois dela, ou fica parado
  enquanto o aviso de passo irreversível espera). Essa espera tem só o prazo da
  própria confirmação, nem o do passo nem o da execução (o lançador mata o
  processo do executor 15 s depois do prazo da execução); se o prazo da execução
  passou durante ela, o passo falha sem agir. Depois da confirmação do aviso de
  passo irreversível, a página pode ter mudado: o alvo, a linha única e a rede
  contra ações repetidas são conferidos de novo, na mesma página da primeira
  conferência, e o passo falha sem agir se a página for outra. Uma página que sai
  e volta durante essa espera (vai a outro endereço e volta ao mesmo) também é
  recusada (perda de disponibilidade aceita): a volta não foi o ``navigate`` do
  robô e conta como da ação anterior na mesma linha. Qualquer falha
  depois do aviso de passo irreversível e dessa conferência deixa a execução
  "indeterminada", nunca "falhou". Um
  contêiner (``for_each``, ``if``) não age e nunca se anuncia como gravação (o
  efeito dele vai em ``efeito_do_bloco``): o efeito dele vale para cada passo de
  dentro que age na página (clicar, digitar, escolher, marcar, teclar, enviar ou
  baixar um arquivo, abrir um endereço), que só sobe, e é cada um destes que se
  anuncia. Um passo de dentro que só lê (``extract_text``, ``extract_table``,
  ``wait_for``, ``assert``) fica com o efeito dele e nunca se anuncia como
  gravação. O efeito de cada passo é o mesmo em tudo: no anúncio, na rede contra
  ações repetidas, nos localizadores alternativos e na conferência antes do
  primeiro passo;
- um passo que grava (``write`` ou ``irreversible``) e age num elemento age no
  elemento conferido, e não no que o localizador encontrar na hora de agir: o do
  Playwright procura de novo, no documento que estiver carregado, enquanto
  espera o elemento ficar habilitado, e cairia na mesma linha de outra página. A
  espera pelo elemento (visível, habilitado e parado; editável para digitar; a
  opção na lista) vem antes, sem mandar nenhuma entrada à página (o clique de
  teste do Playwright manda os eventos do mouse); depois dela, na hora de agir,
  são conferidos de novo o contador de navegações da página principal (que conta
  as de outro documento e as do mesmo documento: ``pushState``,
  ``replaceState``, ``#``), a página e, numa linha escolhida pelo texto, que ela
  ainda é a única com ele e que o elemento conferido ainda está nela (uma lista
  que reaproveita a linha no lugar, com outro texto, é vista). A ação vai em
  seguida, sem ``force``: o Playwright ainda confere, sem esperar de novo o que já
  esperou, que o alvo não está coberto (também por outro documento: um iframe por
  cima do botão, a página por cima de um iframe) nem inerte (um diálogo modal), e
  espera ficar livre. Antes dela, um vigia na fase de captura da janela (com o
  ouvinte posto antes dos scripts de cada documento, e que decide antes dos da
  página) confere o primeiro evento da ação: se ele não cai no alvo (o botão ou
  link em volta dele, num clique; o campo, ao digitar), se o endereço mudou, se a
  linha já não tem um elemento cujo texto inteiro do HTML é a chave (a mesma regra
  da escolha da linha: a linha reaproveitada de "12" para "120" não vale) ou, num
  clique ou numa marcação, se o rótulo do alvo mudou (uma lista virtual que
  reaproveita o botão para outro item), o evento e os seguintes da ação são
  cancelados e o passo falha; um clique cujos eventos não chegam à janela do alvo
  foi entregue a outro documento e falha dizendo que pode ter feito outra coisa.
  Num iframe deslocado ou em escala, onde o Playwright não confere o que está por
  cima do alvo, o clique espera o alvo ficar livre só lendo a página. Uma mudança de endereço (também no mesmo documento) ou de linha falha com
  ``elemento_ambiguo``, e nada é feito; se a página troca de documento durante a
  espera, o elemento sai da página e o passo falha com ``acao_falhou``. Um
  elemento que sai da página durante a espera sem navegação (o botão montado de
  novo na mesma linha) leva a uma nova conferência completa (alvo, página,
  contador, rede e linha única), uma vez; na segunda vez, o passo falha
  (``acao_falhou``). Ficam de fora (resíduos conhecidos) os shadow roots fechados,
  a chave que só o CSS mostra, uma lista virtual que troca o que a linha é sem
  trocar o texto dela, e o clique num iframe girado sem campos digitados noutro
  documento (fica com o Playwright). Se a página muda o endereço, a linha ou o
  rótulo com os primeiros eventos de um clique (no ``pointerdown``), o vigia
  interrompe o clique antes do ``click`` e o passo falha dizendo que esses eventos
  chegaram à página. Perdas de disponibilidade aceitas: qualquer mudança de endereço da
  página principal entre a conferência do alvo e a ação (também um filtro que
  muda a consulta do endereço), e uma tecla para um elemento que não recebe o
  foco. Um segredo vai para o elemento em que a origem foi conferida, sem o
  vigia;
- depois de digitar um texto (``fill``, não um segredo), o valor do campo é
  conferido: se a página o mudou (o CEP que preenche o logradouro quando o foco
  sai dele, e o texto iria para o fim do que ela pôs), o texto é digitado de
  novo, e vale se o campo ficar com ele ou exatamente com o que ficou da
  primeira vez (a formatação da própria página, como uma máscara de CPF);
  senão, o passo falha. Num passo que grava, nada que gravou é repetido: sem
  digitar de novo, vale o texto como o navegador o guarda (posto numa cópia solta
  do campo: ``\\r\\n`` vira ``\\n``, um e-mail ou endereço perde os espaços das
  pontas), com os brancos (e o espaço rígido) normalizados, ou, num texto sem
  letras, a máscara da página (:func:`_typed_value_matches`):
  os mesmos dígitos na mesma ordem (CPF, CNPJ, CEP, telefone, data, centavos),
  com o sinal de menos, e, num número com casas decimais ou com o ponto dos
  milhares, o mesmo número ("10,5" que vira "1,05" não vale). Um campo vazio
  nunca vale por um texto, e num ``input[type=number]`` a regra dos dígitos não
  vale. Qualquer outra diferença (o ``maxlength`` que cortou o texto, uma página
  que troca letras) faz o passo falhar. Os campos de texto que o robô digitou são
  lidos de novo antes de cada passo que grava e age na página, comparados com a
  referência (:meth:`_refresh_typed`): o valor da digitação, ou o do fim do último
  clique, tecla ou escolha do robô. O que muda durante esses passos vale (a
  sugestão escolhida num autocompletar, a busca limpa depois do Enter), só para os
  campos que ainda tinham a referência logo antes de o passo agir; a formatação
  quando o foco sai do campo vale pela comparação; o que a página muda sozinha
  enquanto o robô espera ou digita noutro campo, também depois de um
  ``pushState``/``replaceState`` (uma consulta de CEP que responde depois), faz o
  passo falhar com ``acao_falhou`` sem agir. Num clique, numa marcação ou numa
  escolha, o vigia confere de novo, no primeiro evento, os campos digitados do
  documento do alvo (a página que troca o campo enquanto o clique espera uma camada
  sair); com campos digitados noutro documento, o clique espera o alvo ficar livre
  antes da releitura, só lendo a página (e falha sem agir se o alvo continua coberto
  no fim do prazo), e um primeiro evento que chega mais de 0,5 s depois do vigia (um
  documento de outra origem por cima do alvo, uma camada que chegou depois da espera)
  é recusado. O campo digitado de novo vale pela última vez, e o que saiu da página
  (ou cujo documento acabou) deixa de contar; cada campo é lido no próprio documento.
  Resíduos: a mudança que chega durante um clique, uma tecla ou uma escolha do robô
  vira a referência, e a que chega entre a releitura e a ação de um passo de digitar
  ou de teclar (ou, noutro documento, até 0,5 s depois da releitura) não é vista. Os valores do campo nunca vão para eventos nem
  mensagens;
- valores de entradas, variáveis e itens nunca mudam o alvo de um passo: não
  entram em seletores CSS/XPath, cada um vira um único trecho codificado do
  endereço (no caminho e na rota depois do ``#``, sem barras, sem ``%`` e nunca
  vazio), e o texto que escolhe uma linha precisa ser o texto inteiro de um
  elemento dela (uma célula, por exemplo), com as mesmas maiúsculas e sem
  nenhum valor vazio;
- a linha é escolhida pelo texto do HTML (``textContent``, como o Playwright
  compara), mas ``extract_table`` e ``extract_text`` leem o texto mostrado
  (``innerText``), que pode ser outro: o ``text-transform`` do CSS muda as
  maiúsculas, e um texto escondido na célula (a chave de ordenação do
  DataTables, ``<span hidden>0007</span>7``) só existe no HTML. Por isso a
  linha escolhida precisa ser a **única** com a chave nas duas bases: nenhuma
  outra linha pode ter, no HTML, um elemento com esse texto inteiro, e nenhuma
  outra pode mostrar um elemento com esse texto (o ``innerText`` normalizado
  como acima) nem com o texto que a linha escolhida mostra no lugar dele (a
  célula ``AB3X`` mostrada como ``ab3x`` pelo CSS, ao lado da nota ``ab3x``). Se
  o HTML e o texto mostrado apontam para linhas diferentes, ou para mais de uma,
  o passo falha fechado com ``elemento_ambiguo`` antes de
  agir, e nada é feito; vale também para passos de leitura, cujo valor pode ir
  para um passo que grava, e para condições (``visible``, ``text_contains``):
  uma linha que não é única nunca conta como não encontrada (uma condição
  negada levaria ao ramo que cria a nota), e a condição falha fechado com
  ``elemento_ambiguo``. Na dúvida, uma outra linha mostra a chave se o
  ``innerText``, o ``textContent`` ou o texto montado dos shadow roots abertos
  (que o ``innerText`` não vê) de um elemento dela a mostra, com o ``<br>``
  separando as palavras ou juntando-as; o texto montado nunca substitui o
  ``innerText``. Ficam de fora (resíduo conhecido) os shadow roots fechados e o
  texto que só o CSS mostra, sem nada no HTML (``content`` de um ``::before``).
  Se nenhuma linha tem a chave no HTML, o passo falha
  com ``elemento_nao_encontrado`` (uma chave lida em ``NF 7`` de uma célula
  ``nf 7`` mostrada em maiúsculas não escolhe nada). A comparação nunca ignora
  maiúsculas, para uma chave nunca escolher a linha de outro código;
- numa execução, duas ações que gravam (``write`` ou ``irreversible``) nunca
  caem na mesma linha em voltas diferentes de uma repetição: a linha é a página
  (domínio e caminho do endereço, em minúsculas e sem a barra do fim) e o texto
  que a escolhe, e as páginas de uma ação são a de antes de agir e cada endereço
  por onde a página principal passou depois, até o fim da volta da repetição em
  que ela agiu e, depois dela, até o fim dessa repetição, menos os endereços que o
  próprio passo ``navigate`` do robô abre: a resposta de um formulário que
  mostra a mesma tabela em outro endereço ainda é a mesma linha, quer o
  formulário seja enviado pela ação, quer por um passo seguinte da volta (a
  confirmação de um modal, "Cancelar selecionadas"), e a página que a volta abre
  depois de agir (um link, um ``navigate``) também, assim como o endereço a que
  um temporizador ou um popup da ação leva a página numa volta seguinte. Uma
  página que o robô abre com um ``navigate`` numa volta seguinte, antes de agir
  nela, é outra página; uma página aberta numa volta seguinte por um clique (um
  link) conta como da ação anterior, porque não dá para saber se quem levou a
  página até ela foi o clique ou um temporizador ou popup da ação: na dúvida,
  o passo é recusado. Pelo mesmo motivo, um endereço a que a página aberta por
  um ``navigate`` vai sozinha depois (``replaceState`` ao carregar, ``meta
  refresh``, um redirecionamento por script) também conta como da ação
  anterior. Na segunda vez, o passo
  falha com ``elemento_ambiguo`` antes de agir (e antes do aviso de passo
  irreversível), qualquer que seja o alvo: os dois ramos de um ``if``, por
  localizadores diferentes, e chaves cruzadas entre dois passos também são
  recusados. Na mesma volta (ou fora de repetições), passos diferentes podem
  agir na mesma linha (dois cliques no "+", digitar a quantidade e clicar em
  Salvar), e duas repetições que não estão uma dentro da outra (marcar todas,
  depois cancelar todas) não se cruzam; passos de leitura podem voltar a ela. A
  mesma chave em outra página (a nota 1 de cada filial) é outra linha. É a rede
  de segurança para itens repetidos de uma tabela lida da página;
- o que faria a execução falhar no meio por causa das entradas (campo de item
  ausente, número que não pode ser digitado, lista maior que a repetição, valor
  que não pode ir no caminho ou na rota do endereço, valor vazio no texto que
  escolhe a linha, itens de uma lista de entrada que repetiriam a mesma ação na
  mesma linha) falha antes do primeiro passo. Itens repetidos são os que
  escolhem a mesma linha, pelo mesmo texto normalizado, para a mesma ação que
  grava, e fazem no resto da repetição os mesmos passos com os mesmos valores
  onde isso pode mudar a página (endereços, alvos, opções); numa repetição com
  outras dentro, cada item de fora repete as ações das de dentro (dois meses na
  mesma página cancelariam as mesmas notas). Valores usados só em
  passos que não rodam não contam: um ramo de ``if`` cuja condição é um
  ``equals`` só de entradas e itens de listas de entrada é decidido antes, como
  seria na execução, e uma repetição sobre uma lista de entrada vazia não roda.
  Quando a condição depende da página ou de variáveis, na dúvida o valor é
  recusado antes, e a mensagem diz o passo e o ramo;
- eventos, mensagens, saídas e nomes de artefatos nunca contêm valores de
  credenciais; as chaves dos eventos e os valores que são constantes do
  executor (efeito, estado, código de erro, id de passo) nunca são mascarados.
"""

import asyncio
import math
import os
import re
import unicodedata
import weakref
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, NamedTuple, Self, assert_never, cast
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

from playwright.async_api import (
    BrowserContext,
    ElementHandle,
    Frame,
    FrameLocator,
    JSHandle,
    Page,
    Request,
    Route,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator as PwLocator
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from rapa.core import templates
from rapa.core.contracts import RunMode, RunnerJob, RunState
from rapa.core.events import RunEvent, RunEventKind
from rapa.core.policy import RAW_SELECTORS, TEST_TLD
from rapa.core.robot import (
    AssertStep,
    CheckStep,
    ClickStep,
    Condition,
    CredentialRef,
    DownloadStep,
    Effect,
    EqualsCondition,
    ExtractTableStep,
    ExtractTextStep,
    FieldType,
    FillStep,
    ForEachStep,
    IfStep,
    InputField,
    Locator,
    LocatorBy,
    Manifest,
    NavigateStep,
    PressStep,
    SecretValue,
    SelectStep,
    Step,
    Target,
    TextContainsCondition,
    UploadStep,
    UrlContainsCondition,
    VisibleCondition,
    WaitForStep,
    max_effect,
)
from rapa.runner.errors import ErrorCode, StepError
from rapa.runner.ports import (
    ArtifactSink,
    EventSink,
    FileProvider,
    FileUnavailable,
    SecretProvider,
    SecretUnavailable,
    sanitize_filename,
)
from rapa.runner.result import ArtifactKind, ArtifactRef, RunResult

__all__ = ["MASK", "Interpreter"]

#: Texto que substitui valores de credenciais em eventos, mensagens e saídas.
MASK = "***"

_TIMEOUT_GRACE_S = 2.0
_SCREENSHOT_TIMEOUT_MS = 5_000
_DETAIL_MAX = 300
_MIN_MASKED_LENGTH = 4
#: Campos de evento com hashes que o próprio executor calcula (nunca dados da página).
_HASH_KEYS = frozenset({"sha256", "fingerprint"})
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
#: Campos de evento cujo valor é uma constante do executor, quando é um destes.
_EVENT_CONSTANTS: dict[str, frozenset[str]] = {
    "efeito": frozenset(effect.value for effect in Effect),
    "efeito_do_bloco": frozenset(effect.value for effect in Effect),
    "estado": frozenset(state.value for state in RunState),
    "codigo": frozenset(code.value for code in ErrorCode),
    "modo": frozenset(mode.value for mode in RunMode),
    "estrategia": frozenset(by.value for by in LocatorBy),
    "estrategias": frozenset(by.value for by in LocatorBy),
    "ramo": frozenset({"entao", "senao"}),
    "origem": frozenset({"texto", "segredo"}),
}
#: Campos de evento com o id de um passo do robô.
_STEP_ID_KEYS = ("passo", "passo_irreversivel", "passo_interno")
_DEFAULT_PORTS = {"http": 80, "https": 443}
#: Problemas de entrada listados na mensagem de erro (o resto só é contado).
_MAX_PROBLEMS = 20
#: Maior inteiro que um float guarda sem perder precisão.
_MAX_EXACT_FLOAT_INT = 2**53

#: Brancos do JavaScript (os de ``trim()`` e de ``\s``), que o Playwright usa ao comparar
#: textos. Não são os do Python: U+FEFF entra, U+001C a U+001F e U+0085 não.
_JS_SPACES = re.compile(
    "[\t\n\x0b\x0c\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+"
)
#: Caracteres que o Playwright descarta antes de comparar textos.
_IGNORED_IN_TEXT = re.compile("[\u200b\u00ad]")
# Trechos da expressão do texto inteiro de um elemento (avaliada no navegador).
_TEXT_EDGE = r"[\s\u200b\u00ad]*"
_INSIDE_WORD = r"[\u200b\u00ad]*"
_BETWEEN_WORDS = r"[\u200b\u00ad]*\s[\s\u200b\u00ad]*"

type _Root = Page | FrameLocator | PwLocator
type _Action = Callable[[], Awaitable[dict[str, Any]]]
#: Onde uma referência cai no endereço de um ``navigate``. ``rota`` é o fragmento
#: até um ``?`` dentro dele: em aplicações de página única (``#!/notas/1``) é o caminho.
type _UrlSlot = Literal["autoridade", "caminho", "consulta", "rota", "fragmento"]
#: Trechos onde valem as regras do caminho, e como cada um aparece nas mensagens.
_PATH_SLOTS: dict[_UrlSlot, str] = {
    "caminho": "no caminho do endereço",
    "rota": "na rota do endereço (depois do '#')",
}
#: Itens de uma repetição sobre uma entrada de lista: o nome da lista e as linhas que
#: chegam a um bloco de passos, cada uma com a sua posição na lista.
type _Rows = tuple[str, list[tuple[int, Mapping[str, object]]]]
#: Ramos de ``if`` em volta de um bloco que só durante a execução se sabe se rodam.
type _Undecided = tuple[str, ...]
#: Uma ação que grava numa linha, na conferência antes do primeiro passo: o tipo do passo
#: e o alvo (:func:`_target_signature`) e o texto normalizado que escolhe a linha.
type _RowAction = tuple[object, ...]
#: Passos que só leem a página: não mudam a página nem a linha onde os seguintes agem.
_PAGE_READS = (ExtractTextStep, ExtractTableStep, AssertStep, WaitForStep)
#: Passos que agem num elemento da página (o ``press`` só quando tem alvo).
_ACTS_ON_TARGET = (ClickStep, FillStep, SelectStep, CheckStep, PressStep, UploadStep, DownloadStep)
#: Prazo, em segundos, para ler os campos digitados fora da conferência antes de gravar (no
#: fim de um passo e logo antes de agir): sem resposta, nenhum ganha referência nova.
_TYPED_READ_S = 5.0
#: Com campos digitados noutro documento que o do alvo, o vigia recusa o primeiro evento de
#: um clique, marcação ou escolha que chega mais que isso (em ms) depois dele: o Playwright
#: esperou o alvo ficar livre (uma camada que a espera antes da releitura não viu), e esses
#: campos podem ter mudado nessa espera sem ser vistos.
_LATE_EVENT_MS = 500.0
#: A espera pelo alvo livre antes da releitura desiste (e o passo falha sem agir) quando
#: falta menos que isso (em s) para o fim do prazo do passo: a ação precisa de tempo.
_UNCOVERED_MARGIN_S = 1.0


def _refreshes_typed(step: Step) -> bool:
    """O passo, no fim, dá a referência nova aos campos digitados
    (:meth:`Interpreter._refresh_typed`): age na página sem digitar (um clique, uma tecla,
    uma escolha, um endereço). Os que só leem e os que digitam, não."""
    return not isinstance(step, (*_PAGE_READS, FillStep))


def _held(read: list[tuple[_Typed, str]]) -> set[int]:
    """Os campos que a conferência antes de gravar leu com a referência deles."""
    return {id(entry.handle) for entry, _ in read}


def _effective_effect(step: Step, inherited: Iterable[Effect]) -> Effect:
    """O efeito com que um passo é tratado, dentro dos contêineres de efeito ``inherited``.

    Um passo que age na página (clicar, digitar, escolher, marcar, teclar, enviar ou
    baixar um arquivo, abrir um endereço) e um contêiner ficam com o maior entre o
    efeito deles e o dos contêineres em volta: o efeito só sobe, e na dúvida um passo
    dentro de um bloco que grava também grava. Um passo que só lê a página
    (:data:`_PAGE_READS`) nunca age nela e fica com o seu. É esse efeito que vale em
    tudo: no anúncio (e na confirmação), na rede contra ações repetidas, na regra dos
    localizadores alternativos e na conferência antes do primeiro passo.
    """
    if isinstance(step, _PAGE_READS):
        return step.effect
    return max_effect(step.effect, *inherited)


#: Contextos já usados por alguma execução: nunca se reaproveita um contexto.
_claimed_contexts: weakref.WeakSet[BrowserContext] = weakref.WeakSet()

# O texto de um campo que é um elemento editável (``contenteditable``), cujo valor é o
# texto mostrado; ``null`` nos outros campos.
_EDITABLE_TEXT_JS = "(element) => (element.isContentEditable === true ? element.innerText : null)"

#: Fora dos dígitos ASCII (a máscara de um campo só muda o resto).
_NOT_DIGIT = re.compile(r"[^0-9]")

# O campo de um elemento como o Playwright o encontra para digitar ou ler o valor
# (``retarget`` com ``follow-label``): um rótulo leva ao campo dele.
_FIELD_OF_FN = r"""
const fieldOf = (element) => {
  let field = element;
  if (!field.matches('input, textarea, select') && !field.isContentEditable) {
    field = field.closest('button, [role=button], [role=checkbox], [role=radio]') || field;
  }
  if (!field.matches('a, input, textarea, button, select, option, optgroup, [role=link], '
      + '[role=button], [role=checkbox], [role=radio]') && !field.isContentEditable) {
    const label = field.closest('label');
    if (label && label.control) field = label.control;
  }
  return field;
};
"""

# [valor do campo, se ele é um elemento editável, o valor que o navegador dá ao texto, se é
# um ``input[type=number]``]: o texto posto numa cópia solta do campo (``cloneNode``), que o
# navegador guarda como guardaria no campo (``\r\n`` vira ``\n``, um e-mail ou endereço perde
# os espaços das pontas). O ``maxlength`` só limita o que se digita, não o que um script
# põe: a cópia fica com o texto inteiro.
_FIELD_STATE_FN = (
    _FIELD_OF_FN
    + r"""
const fieldState = (element, text) => {
  if (element.isContentEditable === true) return [element.innerText, true, null, false];
  const field = fieldOf(element);
  // Pelo nome da etiqueta, e não por instanceof: o campo pode ser de outro documento (um
  // iframe), com outras classes.
  const tag = field.tagName.toUpperCase();
  if (tag !== 'INPUT' && tag !== 'TEXTAREA' && tag !== 'SELECT') {
    return [null, false, null, false];
  }
  let browser = null;
  if (tag !== 'SELECT') {
    try {
      const copy = field.cloneNode(false);
      copy.value = text;
      browser = copy.value;
    } catch (error) {
      browser = null;
    }
  }
  return [field.value, false, browser, tag === 'INPUT' && field.type === 'number'];
};
"""
)
_TYPED_STATE_JS = "(element, text) => {" + _FIELD_STATE_FN + "return fieldState(element, text);}"
# Os campos de texto já digitados (:meth:`Interpreter._typed_fields_hold`), cada um com o
# estado de :data:`_TYPED_STATE_JS`: ``null`` se saiu da página, ``'mesmo'`` se é o campo em
# que o passo vai digitar agora.
_TYPED_FIELD_JS = (
    "(element, [text, current]) => {"
    + _FIELD_STATE_FN
    + "if (element === current) return 'mesmo';"
    + "return element.isConnected ? fieldState(element, text) : null;}"
)
_TYPED_FIELDS_JS = (
    "(_, [fields, texts, current]) => {"
    + _FIELD_STATE_FN
    + "return fields.map((element, index) => (element === current ? 'mesmo'"
    + " : element.isConnected ? fieldState(element, texts[index]) : null));}"
)
_SAME_ELEMENTS_JS = "(element, others) => others.map((other) => other === element)"
_SAME_ELEMENT_JS = "(element, other) => element === other"
# Se a lista já tem a opção pelo rótulo, como o Playwright a escolhe (o rótulo exato ou com
# os brancos normalizados). Num elemento que não é uma lista, o próprio Playwright dá o erro.
_HAS_OPTION_JS = (
    "(element, label) => {"
    + _FIELD_OF_FN
    + r"""
const select = fieldOf(element);
if (!(select instanceof HTMLSelectElement)) return true;
const normalize = (text) => text.replace(/[​­]/g, '').trim().replace(/\s+/g, ' ');
return Array.from(select.options).some(
  (option) => option.label === label || normalize(option.label) === normalize(label));
}"""
)

# Lê as linhas de dados de uma tabela HTML (ou de uma grade ARIA), já com o
# texto de cada célula aparado. Uma ida e volta ao navegador por tabela.
_READ_TABLE_JS = """
(el, maxRows) => {
  const text = (node) => (node.innerText ?? node.textContent ?? '').trim();
  if (el.tagName === 'TABLE') {
    let headers = [];
    const head = el.tHead;
    if (head && head.rows.length) headers = Array.from(head.rows[head.rows.length - 1].cells);
    let rows = el.tBodies.length
      ? Array.from(el.tBodies).flatMap((body) => Array.from(body.rows))
      : Array.from(el.rows).filter((row) => !row.closest('thead') && !row.closest('tfoot'));
    if (!headers.length && rows.length && Array.from(rows[0].cells).every((c) => c.tagName === 'TH')) {
      headers = Array.from(rows[0].cells);
      rows = rows.slice(1);
    }
    rows = rows.filter((row) => row.cells.length > 0);
    return {
      headers: headers.map(text),
      rows: rows.slice(0, maxRows).map((row) => Array.from(row.cells).map(text)),
      total: rows.length,
    };
  }
  const all = Array.from(el.querySelectorAll('[role="row"]'));
  const isHeader = (row) => row.querySelector('[role="columnheader"]') !== null;
  const headerRow = all.find(isHeader);
  const cells = (row) =>
    Array.from(row.querySelectorAll('[role="cell"],[role="gridcell"],[role="rowheader"]'));
  const rows = all.filter((row) => !isHeader(row) && cells(row).length > 0);
  return {
    headers: headerRow
      ? Array.from(headerRow.querySelectorAll('[role="columnheader"]')).map(text)
      : [],
    rows: rows.slice(0, maxRows).map((row) => cells(row).map(text)),
    total: rows.length,
  };
}
"""

# Quantos contêineres, fora o escolhido pelo texto do HTML, mostram a chave ou o que o
# escolhido mostra no lugar dela: têm um elemento (ou são um) cujo texto mostrado,
# normalizado como o Playwright normaliza, é a chave inteira ou o texto mostrado por um
# elemento do escolhido cujo texto do HTML é a chave (a célula "AB3X" mostrada como
# "ab3x" pelo CSS). -1 se o escolhido não está entre os contêineres. O texto do HTML é o
# que o Playwright compara: o dos filhos e o de shadow roots abertos, sem scripts e
# estilos, e o valor de botões <input>. Cada elemento é visto uma vez, e quem mostra um
# desses textos marca os elementos que o contêm.
#
# O texto mostrado é o innerText, que não entra em shadow roots (o de um elemento que só
# tem um shadow root é vazio). Nos elementos com um shadow root aberto dentro (ou que
# são um), ele também é montado à parte, como a página mostra: o conteúdo do shadow root
# no lugar dos filhos (os filhos só pelos slots), o innerText dos elementos de dentro,
# uma quebra de linha por <br>, e o texto solto com o text-transform de quem o mostra.
# Com outro text-transform (capitalize, por exemplo), esse texto é comparado sem olhar
# maiúsculas: na dúvida, mostra a mesma chave. Só esses elementos pagam a montagem, que
# nunca substitui o innerText: um elemento mostra a chave se o innerText, o textContent
# ou o texto montado dele a mostram.
_OTHER_ROWS_SHOWING_KEY_JS = r"""
(rows, [key, chosen]) => {
  if (!rows.includes(chosen)) return -1;
  const normalize = (text) => text.replace(/[\u200b\u00ad]/g, '').replace(/\s+/g, ' ').trim();
  const skipped = new Set(['SCRIPT', 'NOSCRIPT', 'STYLE']);
  const buttons = new Set(['submit', 'button', 'reset']);
  // Elementos (e shadow roots) com um shadow root aberto dentro, ou que são um, e os
  // slots (que mostram filhos de outro elemento).
  const withShadow = new Set();
  const findHosts = (root) => {
    for (const element of [root, ...root.querySelectorAll('*')]) {
      const slot = element instanceof HTMLSlotElement;
      if (!element.shadowRoot && !slot) continue;
      for (let at = element; at && !withShadow.has(at); at = at.parentNode ?? at.host) {
        withShadow.add(at);
      }
      if (element.shadowRoot) findHosts(element.shadowRoot);
    }
  };
  for (const row of rows) findHosts(row);
  // O texto solto que ``owner`` mostra (nada, se ele está invisível).
  const transformed = (text, owner) => {
    const style = getComputedStyle(owner);
    if (style.visibility !== 'visible') return ['', false];
    switch (style.textTransform) {
      case 'none':
        return [text, false];
      case 'uppercase':
        return [text.toUpperCase(), false];
      case 'lowercase':
        return [text.toLowerCase(), false];
      default:
        return [text, true];
    }
  };
  const composedTexts = new Map();
  const innerText = (element) =>
    typeof element.innerText === 'string' ? element.innerText : element.textContent ?? '';
  // [texto mostrado, se as maiúsculas dele são incertas, o mesmo texto com cada <br>
  // juntando as palavras]
  const shownParts = (element) => {
    if (!withShadow.has(element)) {
      const text = innerText(element);
      return [text, false, text];
    }
    let parts = composedTexts.get(element);
    if (parts !== undefined) return parts;
    let text = '';
    let joined = '';
    let loose = false;
    let children = element.shadowRoot ? element.shadowRoot.childNodes : element.childNodes;
    if (element instanceof HTMLSlotElement && element.assignedNodes({ flatten: true }).length) {
      children = element.assignedNodes({ flatten: true });
    }
    for (const child of children) {
      if (child.nodeType === 3) {
        const [piece, uncertain] = transformed(child.nodeValue ?? '', element);
        text += piece;
        joined += piece;
        loose ||= uncertain;
      } else if (child.nodeName === 'BR') {
        text += '\n';  // o innerText de um <br> é vazio, mas ele separa as palavras
      } else if (child.nodeType === 1 && !skipped.has(child.nodeName)) {
        const display = getComputedStyle(child).display;
        if (display === 'none') continue;
        const [piece, uncertain, pieceJoined] = shownParts(child);
        const inline = display.startsWith('inline') || display === 'contents';
        text += inline ? piece : ` ${piece} `;
        joined += inline ? pieceJoined : ` ${pieceJoined} `;
        loose ||= uncertain;
      }
    }
    parts = [text, loose, joined];
    composedTexts.set(element, parts);
    return parts;
  };
  // Os textos que um elemento pode estar mostrando, cada um com a incerteza das
  // maiúsculas: o innerText, o texto montado (com um shadow root aberto dentro), lido
  // com o <br> separando as palavras e também juntando-as, e, nas outras linhas, o
  // textContent. Na dúvida, qualquer um deles.
  const shownTexts = (element, withContent) => {
    const texts = [[innerText(element), false]];
    if (withShadow.has(element)) {
      const [text, loose, joined] = shownParts(element);
      texts.push([text, loose], [joined, loose]);
    }
    if (withContent) texts.push([element.textContent ?? '', false]);
    return texts;
  };
  const htmlTexts = new Map();
  const htmlText = (node) => {
    let text = htmlTexts.get(node);
    if (text !== undefined) return text;
    text = '';
    if (node.nodeType === 1 && skipped.has(node.nodeName)) {
      text = '';
    } else if (node instanceof HTMLInputElement && buttons.has(node.type)) {
      text = node.value;
    } else {
      for (let child = node.firstChild; child; child = child.nextSibling) {
        if (child.nodeType === 3) text += child.nodeValue ?? '';
        else if (child.nodeType === 1) text += htmlText(child);
      }
      if (node.shadowRoot) text += htmlText(node.shadowRoot);
    }
    htmlTexts.set(node, text);
    return text;
  };
  const wanted = new Set([key]);
  let looseWanted = false;
  const inChosen = [chosen];
  while (inChosen.length) {
    const node = inChosen.pop();
    if (node.nodeType === 1) {
      if (skipped.has(node.nodeName)) continue;
      if (normalize(htmlText(node)) === key) {
        for (const [raw, loose] of shownTexts(node, false)) {
          const text = normalize(raw);
          if (text) {  // uma chave escondida não mostra nada
            wanted.add(text);
            looseWanted ||= loose;
          }
        }
      }
      if (node.shadowRoot) inChosen.push(node.shadowRoot);
    }
    for (const child of node.children) inChosen.push(child);
  }
  const folded = new Set(Array.from(wanted, (text) => text.toLowerCase()));
  const shows = (node) =>
    shownTexts(node, true).some(([raw, loose]) => {
      const text = normalize(raw);
      return wanted.has(text) || ((loose || looseWanted) && folded.has(text.toLowerCase()));
    });
  const seen = new Set();
  const marked = new Set();
  const mark = (node) => {
    for (let at = node; at && !marked.has(at); at = at.parentNode ?? at.host) marked.add(at);
  };
  for (const row of rows) {
    const pending = [row];
    while (pending.length) {
      const node = pending.pop();
      if (seen.has(node)) continue;
      seen.add(node);
      if (node.nodeType === 1) {
        if (skipped.has(node.nodeName)) continue;
        if (shows(node)) mark(node);
        if (node.shadowRoot) pending.push(node.shadowRoot);
      }
      for (const child of node.children) pending.push(child);
    }
  }
  return rows.filter((row) => row !== chosen && marked.has(row)).length;
}
"""

# Na hora de agir: a contagem de :data:`_OTHER_ROWS_SHOWING_KEY_JS`, se o elemento conferido
# ainda está dentro do contêiner escolhido; -1 se não está (a página pôs outro texto na linha
# dele, ou o levou para outra linha).
_PINNED_ROW_JS = (
    "(rows, [key, chosen, element]) => {"
    " for (let at = element; at; at = at.parentNode ?? at.host) {"
    "  if (at === chosen) return (" + _OTHER_ROWS_SHOWING_KEY_JS.strip() + ")(rows, [key, chosen]);"
    " }"
    " return -1;"
    "}"
)

#: Os eventos que o vigia de um passo que grava confere (:data:`_DISPATCH_GUARD_JS`).
_GUARD_TYPES_JS = (
    "['pointerover', 'mouseover', 'pointermove', 'mousemove', 'pointerdown', 'mousedown', "
    "'pointerup', 'mouseup', 'click', 'keydown', 'keypress', 'keyup', 'beforeinput', "
    "'input', 'change']"
)

# Posto em cada documento antes de qualquer script dele (:meth:`Interpreter._open_page`): um
# ouvinte na fase de captura da janela, para cada evento que o vigia confere, que passa o
# evento ao vigia do passo, quando há um. Assim o vigia decide antes de qualquer ouvinte da
# própria página, também dos que ela põe na janela, na fase de captura, quando carrega: o
# evento que ele recusa não chega a nenhum. ``document.open()`` (também o ``document.write``
# depois de a página carregar) tira da janela os ouvintes dela, mas a janela e o símbolo
# ficam: cada vigia põe de novo os ouvintes do gancho (com o ``addEventListener`` de antes
# dos scripts da página; sem efeito quando eles ainda estão lá).
_GUARD_HOOK_JS = (
    "(() => {\nconst types = "
    + _GUARD_TYPES_JS
    + r""";
const key = Symbol.for('rapa.vigia');
if (Object.prototype.hasOwnProperty.call(window, key)) return;
let current = null;
const forward = (event) => {
  if (current !== null) current(event);
};
const listen = EventTarget.prototype.addEventListener;
const attach = () => {
  for (const type of types) listen.call(window, type, forward, true);
};
attach();
Object.defineProperty(window, key, {
  value: Object.freeze({
    set: (listener) => {
      attach();
      current = listener;
    },
    clear: (listener) => {
      if (current === listener) current = null;
    },
  }),
});
})();"""
)

# O vigia de um passo que grava, posto na janela do elemento conferido logo antes de agir:
# um ouvinte na fase de captura que decide no primeiro evento da ação (o do mouse, de
# qualquer elemento; o do teclado ou da digitação, também sintético quando é no alvo; o
# ``change`` só no alvo, porque o de outro campo que perde o foco não é da ação). O evento
# vale se cai no alvo (o elemento, ou o botão ou link em volta dele num clique, como o
# Playwright confere), com o mesmo endereço da conferência e, numa linha escolhida pelo
# texto, com o elemento ainda dentro dela e um elemento dela cujo texto do HTML, inteiro, é a
# chave (a mesma regra da escolha da linha), e com os campos digitados deste documento ainda
# com o valor que a conferência deles leu (``typed``: a página pode mudá-los enquanto o
# Playwright espera o alvo ficar livre) e, se há campos digitados noutro documento (que o
# vigia não lê), até ``late`` ms depois de o vigia ser posto. Então o vigia sai. Um clique
# cujos eventos não chegaram à janela do alvo (o Playwright clicou noutro documento por cima
# dele) também conta como recusa; marcar uma caixa que já está como deve ficar não manda
# evento nenhum (``marca``) e não é recusa. ``stop()`` devolve o motivo da recusa (``alvo``,
# ``campo`` ou ``espera``) ou ``null``. Senão, ele cancela esse evento e todos os seguintes
# da ação (``preventDefault`` e ``stopImmediatePropagation``) e marca a recusa, que o
# executor lê depois de agir. O ouvinte do vigia é o que :data:`_GUARD_HOOK_JS` pôs no
# documento antes dos scripts dele (senão, um posto agora, depois dos que a página já pôs na
# janela). Num clique ou numa marcação, o rótulo do alvo (``aria-label``, ``title``, o texto
# de ``aria-labelledby`` e dos ``<label>`` dele, ``data-testid``, o texto do HTML com o ``alt``
# das imagens e o ``aria-label`` dos elementos de dentro, como o de um ``<svg>``) também
# precisa ser o da hora em que o vigia foi posto: uma lista virtual que reaproveita o nó para
# outro item enquanto o Playwright rola até ele troca o rótulo. Vale também o rótulo que os
# ouvintes da página para a chegada do mouse ao alvo puseram (``pointerover`` e ``mouseover``,
# que o vigia vê antes deles; o rótulo é lido de novo no ``pointermove`` e no ``mousemove``,
# depois deles), se ele era o da hora do vigia quando o mouse chegou e se só a dica mudou: o
# ``title`` que sai ou chega e o ``aria-label`` que chega igual ao ``title`` da chegada, sem
# que um ou outro tenha passado por outro valor desde o vigia; o texto, os ``<label>``, o
# ``aria-labelledby``, o ``data-testid`` e os nomes de dentro iguais. Depois do primeiro evento de um clique ou
# de uma marcação, e até o ``click``, o vigia confere de novo, em cada evento do navegador, o
# endereço, a linha e o rótulo (os ouvintes da página já receberam os eventos de antes e podem
# tê-los mudado); se mudaram, ele cancela esse evento e os seguintes (``meio``). Um clique
# cujos eventos não chegaram à janela do alvo vale ``fora``.
_DISPATCH_GUARD_JS = (
    "(element, [kind, row, key, typed, late]) => {"
    + _FIELD_OF_FN
    + r"""
const view = element.ownerDocument.defaultView;
const topHref = () => {
  try {
    return view.top.location.href;
  } catch (error) {
    return null;
  }
};
const href = view.location.href;
const top = topHref();
let target = element;
if (kind === 'clique' || kind === 'marca') {
  if (!target.matches('input, textarea, select') && !target.isContentEditable) {
    target = target.closest('button, [role=button], a, [role=link]') || target;
  }
} else if (kind !== 'tecla') {
  target = fieldOf(element);
  while (kind === 'texto' && target.isContentEditable && target.parentElement
      && target.parentElement.isContentEditable) {
    target = target.parentElement;
  }
}
const normalize = (text) => text.replace(/[​­]/g, '').replace(/\s+/g, ' ').trim();
const skipped = new Set(['SCRIPT', 'NOSCRIPT', 'STYLE']);
const buttons = new Set(['submit', 'button', 'reset']);
const htmlText = (node) => {
  if (node.nodeType === 1 && skipped.has(node.nodeName)) return '';
  if (node instanceof HTMLInputElement && buttons.has(node.type)) return node.value;
  let text = '';
  for (let child = node.firstChild; child; child = child.nextSibling) {
    if (child.nodeType === 3) text += child.nodeValue ?? '';
    else if (child.nodeType === 1) text += htmlText(child);
  }
  if (node.shadowRoot) text += htmlText(node.shadowRoot);
  return text;
};
const moved = () => {
  if (view.location.href !== href) return true;
  if (top === null) return false;
  const now = topHref();
  return now === null || now !== top;
};
const hasWhole = (node) => {
  if (node.nodeType === 1 && skipped.has(node.nodeName)) return false;
  if (node.nodeType === 1 && normalize(htmlText(node)) === key) return true;
  for (let child = node.firstChild; child; child = child.nextSibling) {
    if (child.nodeType === 1 && hasWhole(child)) return true;
  }
  return !!node.shadowRoot && hasWhole(node.shadowRoot);
};
const inRow = () => {
  if (!row) return true;
  let at = element;
  while (at && at !== row) at = at.parentNode ?? at.host;
  return at === row && hasWhole(row);
};
// Os campos digitados deste documento ainda têm o valor que a conferência leu.
const fieldValue = (node) => {
  if (!node.isConnected) return null;
  if (node.isContentEditable === true) return node.innerText;
  const field = fieldOf(node);
  return typeof field.value === 'string' ? field.value : null;
};
const typedHold = () => (typed || []).every(([node, value]) => fieldValue(node) === value);
// Com campos digitados noutro documento, que o vigia não lê: o primeiro evento não demorou.
const armedAt = view.performance.now();
const prompt = () => late === null || view.performance.now() - armedAt <= late;
// O rótulo do alvo num clique ou numa marcação (uma lista virtual que reaproveita o nó).
const named = (ids) => (ids ?? '').split(/\s+/).filter(Boolean)
  .map((id) => target.getRootNode().getElementById?.(id) ?? null)
  .map((node) => (node ? normalize(htmlText(node)) : '')).join(' ');
// O texto do nome também vem do alt (<img>, <area>, <input type=image>) e do aria-label de
// um elemento de dentro (o <svg role=img aria-label>).
const inner = [];
const nameText = (node) => {
  if (node.nodeType === 1 && skipped.has(node.nodeName)) return '';
  if (node instanceof HTMLInputElement && buttons.has(node.type)) return node.value;
  if ((node instanceof HTMLInputElement && node.type === 'image') || node instanceof HTMLImageElement
      || node instanceof HTMLAreaElement) {
    return node.getAttribute('alt') ?? '';
  }
  let text = '';
  for (let child = node.firstChild; child; child = child.nextSibling) {
    if (child.nodeType === 3) text += child.nodeValue ?? '';
    else if (child.nodeType === 1) {
      const own = child.getAttribute('aria-label');
      if (own) inner.push(own);
      text += nameText(child);
    }
  }
  if (node.shadowRoot) text += nameText(node.shadowRoot);
  return text;
};
const parts = () => {
  inner.length = 0;
  const text = normalize(nameText(target));
  return [target.getAttribute('aria-label') ?? '', target.getAttribute('title') ?? '',
    named(target.getAttribute('aria-labelledby')),
    [...(target.labels ?? [])].map((node) => normalize(htmlText(node))).join(' '),
    target.getAttribute('data-testid') ?? '', text, inner.join(' ')];
};
const label = () => JSON.stringify(parts());
// Só a dica mudou: o title que sai ou chega, o aria-label que chega igual ao title da chegada
// (o Bootstrap 5); o resto, igual. Os valores por que o title e o aria-label do alvo passaram
// desde o vigia também contam: um nó reaproveitado nos ouvintes da chegada, antes de uma dica
// que tira o title (o jQuery UI), passa pelo title do outro item.
const seen = [];
const keep = (records) => {
  for (const record of records) seen.push([record.attributeName, record.oldValue ?? '']);
};
const watch = (kind === 'clique' || kind === 'marca') && typeof MutationObserver === 'function'
  ? new MutationObserver(keep) : null;
if (watch !== null) {
  watch.observe(target, {attributes: true, attributeOldValue: true,
    attributeFilter: ['title', 'aria-label']});
}
const drain = () => {
  if (watch !== null) keep(watch.takeRecords());
};
const passed = (name, ...allowed) => seen.every(([n, v]) => n !== name || allowed.includes(v));
const tooltipOnly = (h, a) => (drain(), true)
  && (a[0] === h[0] || (h[0] === '' && a[0] === h[1]))
  && passed('aria-label', h[0], '') && passed('title', h[1], '')
  && (a[1] === h[1] || a[1] === '' || h[1] === '')
  && a[2] === h[2] && a[3] === h[3] && a[4] === h[4] && a[5] === h[5]
  && (a[6] === h[6] || h[6] === '');
const armedLabel = kind === 'clique' || kind === 'marca' ? label() : null;
// O rótulo quando o mouse chega ao alvo, antes dos ouvintes da página, e logo depois dos
// ouvintes dela para a chegada: a mudança que a chegada do mouse fez só vale se for a de
// uma dica (que tira o ``title`` ou põe o aria-label). Um texto, um ``<label>`` ou um
// aria-label que muda é o nó reaproveitado para outro item.
let hovered = null;
let afterHover = null;
const sameLabel = () => {
  if (armedLabel === null) return true;
  const now = label();
  return now === armedLabel
    || (hovered === armedLabel && afterHover !== null && now === afterHover
      && tooltipOnly(JSON.parse(hovered), JSON.parse(afterHover)));
};
const hoverTypes = new Set(['pointerover', 'mouseover']);
const moveTypes = new Set(['pointermove', 'mousemove']);
const types = """
    + _GUARD_TYPES_JS
    + r""";
const hook = view[Symbol.for('rapa.vigia')] ?? null;
let state = 'armado';
let reason = 'alvo';
const off = () => {
  if (watch !== null) watch.disconnect();
  if (hook !== null) hook.clear(listener);
  else for (const type of types) view.removeEventListener(type, listener, true);
};
// Num clique ou numa marcação, depois do primeiro evento e até o ``click``: os ouvintes da
// página já receberam os eventos de antes e podem ter mudado o endereço, a linha ou o rótulo.
const pointer = kind === 'clique' || kind === 'marca';
const done = () => {
  state = 'feito';
  off();
};
const listener = (event) => {
  if (state === 'feito') return;
  if (hoverTypes.has(event.type) || moveTypes.has(event.type)) {
    // A chegada do mouse não age: o vigia só guarda o rótulo.
    if (state === 'armado' && armedLabel !== null && event.isTrusted
        && event.composedPath().includes(target)) {
      if (hoverTypes.has(event.type)) {
        if (hovered === null) hovered = label();
      } else if (hovered !== null) {
        afterHover = label();
      }
    }
    return;
  }
  if (state === 'passou') {
    if (!event.isTrusted) return;
    if (!moved() && inRow() && sameLabel()) {
      if (event.type === 'click') done();
      return;
    }
    state = 'recusou';
    reason = 'meio';
  } else if (state === 'armado') {
    const onTarget = event.composedPath().includes(target);
    if (!onTarget && (event.type === 'change' || !event.isTrusted)) return;
    if (onTarget && !moved() && inRow() && sameLabel()) {
      const hold = typedHold();
      if (hold && prompt()) {
        if (pointer && event.type !== 'click') state = 'passou';
        else done();
        return;
      }
      reason = hold ? 'espera' : 'campo';
    }
    state = 'recusou';
  }
  event.preventDefault();
  event.stopImmediatePropagation();
};
if (hook !== null) hook.set(listener);
else for (const type of types) view.addEventListener(type, listener, true);
return {
  stop: () => {
    off();
    if (state === 'recusou') return reason;
    return state === 'armado' && kind === 'clique' ? 'fora' : null;
  },
};
}"""
)


# Se um clique no centro do elemento (da primeira caixa dele na janela) chegaria a ele: o
# elemento do ponto, também dentro de shadow roots abertos, é ele, algo dentro dele ou o
# hospedeiro de um shadow root fechado em volta dele (nunca só um elemento em volta dele: a
# camada com ``pointer-events: none`` ou ``inert`` deixa o ponto cair no que está atrás);
# e, num iframe da mesma origem, o elemento do ponto no documento de cima é o iframe, e
# assim por diante (o ponto vai para o documento de cima pela caixa do iframe, também
# deslocado ou em escala por uma transformação, dele ou de um elemento em volta dele, também
# através de shadow roots; a caixa do elemento vai junto, cortada pela janela de cada
# documento, e o ponto é o centro dela). O texto de um botão que vem de um ``<slot>`` sobe
# pelo slot. Fora da janela do documento dele ou de um documento de cima (uma gaveta que
# ainda entra), ainda não dá para clicar: vale ``false``. Num documento de outra origem acima,
# não dá para saber: vale ``true`` (o vigia confere a demora do primeiro evento). Num iframe
# girado, inclinado, espelhado ou em perspectiva, o ponto de dentro não se traduz pela caixa:
# vale ``null``.
_UNCOVERED_JS = r"""(element) => {
let want = element;
if (!want.matches('input, textarea, select') && !want.isContentEditable) {
  want = want.closest('button, [role=button], a, [role=link]') || want;
}
let view = element.ownerDocument.defaultView;
let box = null;
for (const rect of element.getClientRects()) {
  const left = Math.max(rect.left, 0);
  const top = Math.max(rect.top, 0);
  const right = Math.min(rect.right, view.innerWidth);
  const bottom = Math.min(rect.bottom, view.innerHeight);
  if (right - left >= 1 && bottom - top >= 1) {
    box = [left, top, right, bottom];
    break;
  }
}
if (box === null) return false;
let x = (box[0] + box[2]) / 2;
let y = (box[1] + box[3]) / 2;
const up = (node) => node.parentNode ?? node.host ?? null;
// Só deslocamento e escala positiva, sem girar, inclinar, espelhar nem perspectiva que mude
// o plano do iframe (m31, m32 e m34 só multiplicam z, que é 0 nele: "perspective(1px)
// translateZ(0)", contra o borrão do texto, vale).
const near = (value) => Math.abs(value) < 1e-6;
const flat = (style) => {
  if (style.rotate && style.rotate !== 'none'
      && parseFloat(style.rotate.split(' ').pop()) !== 0) {
    return false;
  }
  if (style.scale && style.scale !== 'none'
      && style.scale.split(' ').some((part) => !(parseFloat(part) > 0))) {
    return false;
  }
  if (style.transform === 'none') return true;
  let m;
  try {
    m = new DOMMatrixReadOnly(style.transform);
  } catch (error) {
    return false;
  }
  return m.m11 > 0 && m.m22 > 0 && near(m.m12) && near(m.m21) && near(m.m13)
    && near(m.m23) && near(m.m14) && near(m.m24) && near(m.m44 - 1);
};
for (;;) {
  const doc = want.ownerDocument;
  let hit = doc.elementFromPoint(x, y);
  while (hit && hit.shadowRoot) {
    const inner = hit.shadowRoot.elementsFromPoint(x, y)[0];
    if (!inner || inner === hit) break;
    hit = inner;
  }
  if (!hit) return false;
  let at = hit;
  while (at && at !== want) at = at.assignedSlot ?? up(at);
  if (at !== want) {
    // O ponto cai no hospedeiro de um shadow root fechado em volta do alvo (o caminho do
    // alvo para cima entra nele por um shadow root que não é visto): não dá para ver dentro.
    let below = want;
    let around = up(want);
    while (around && around !== hit) {
      below = around;
      around = up(around);
    }
    if (!(around === hit && below.nodeType === 11 && below.host === hit
        && hit.shadowRoot === null)) {
      return false;
    }
  }
  let frame = null;
  try {
    frame = doc.defaultView.frameElement;
  } catch (error) {
    frame = null;
  }
  if (!frame) return true;
  const outer = frame.ownerDocument.defaultView;
  for (let node = frame; node && node.nodeType === 1;
      node = node.parentElement ?? node.parentNode?.host ?? null) {
    if (!flat(outer.getComputedStyle(node))) return null;
  }
  const rect = frame.getBoundingClientRect();
  const style = outer.getComputedStyle(frame);
  const sx = frame.offsetWidth ? rect.width / frame.offsetWidth : 1;
  const sy = frame.offsetHeight ? rect.height / frame.offsetHeight : 1;
  const ox = rect.left + (frame.clientLeft + (parseFloat(style.paddingLeft) || 0)) * sx;
  const oy = rect.top + (frame.clientTop + (parseFloat(style.paddingTop) || 0)) * sy;
  box = [Math.max(ox + box[0] * sx, 0), Math.max(oy + box[1] * sy, 0),
    Math.min(ox + box[2] * sx, outer.innerWidth), Math.min(oy + box[3] * sy, outer.innerHeight)];
  // Fora da janela de cima (uma gaveta que ainda entra): ainda não dá para clicar.
  if (box[2] - box[0] < 1 || box[3] - box[1] < 1) return false;
  x = (box[0] + box[2]) / 2;
  y = (box[1] + box[3]) / 2;
  want = frame;
}
}"""


# Se o elemento está num iframe transformado (dele ou de um elemento em volta dele, também
# através de shadow roots, num documento da mesma origem acima): o Playwright não confere o
# que está por cima de um alvo nele.
_IN_TRANSFORMED_FRAME_JS = r"""(element) => {
const moved = (style) => style.transform !== 'none' || (style.rotate && style.rotate !== 'none')
  || (style.scale && style.scale !== 'none')
  || (style.translate && style.translate !== 'none');
let view = element.ownerDocument.defaultView;
for (;;) {
  let frame = null;
  try {
    frame = view.frameElement;
  } catch (error) {
    return false;
  }
  if (!frame) return false;
  const outer = frame.ownerDocument.defaultView;
  for (let node = frame; node && node.nodeType === 1;
      node = node.parentElement ?? node.parentNode?.host ?? null) {
    if (moved(outer.getComputedStyle(node))) return true;
  }
  view = outer;
}
}"""


#: Sem animação (``behavior: 'instant'``), também numa página com ``scroll-behavior: smooth``.
_SCROLL_INTO_VIEW_JS = (
    "(element, align) => element.scrollIntoView({block: align, inline: align, behavior: 'instant'})"
)


class _StopBeforeWrite(Exception):
    def __init__(self, step: Step) -> None:
        super().__init__(step.id)
        self.step = step


class _Cancelled(Exception):
    def __init__(self, step: Step) -> None:
        super().__init__(step.id)
        self.step = step


class _Redactor:
    """Mascara valores de credenciais em qualquer texto que saia do executor."""

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values: set[str] = set()
        for value in values:
            self.add(value)

    def add(self, value: str) -> None:
        # Valores muito curtos mascarariam texto comum demais.
        if len(value) >= _MIN_MASKED_LENGTH:
            self._values.add(value)

    def text(self, text: str) -> str:
        for value in sorted(self._values, key=len, reverse=True):
            if value in text:
                text = text.replace(value, MASK)
        return text

    def value(self, value: Any) -> Any:
        """Mascara textos em qualquer profundidade, inclusive as chaves dos dicionários
        (cabeçalhos de tabela viram chaves). Nenhuma chave fica de fora."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            masked: dict[Any, Any] = {}
            for key, item in value.items():
                name = self._free_key(self.text(key), masked) if isinstance(key, str) else key
                masked[name] = self.value(item)
            return masked
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value

    @staticmethod
    def _free_key(key: str, taken: Mapping[Any, Any]) -> str:
        # Duas chaves que só diferem por um segredo ficariam iguais depois da máscara.
        candidate, n = key, 1
        while candidate in taken:
            n += 1
            candidate = f"{key}_{n}"
        return candidate


class _RowChoice(NamedTuple):
    """Os contêineres de um alvo e os que têm, no HTML, o texto que escolhe a linha."""

    rows: PwLocator
    chosen: PwLocator
    key: str


class _RowIssue(NamedTuple):
    """Por que a linha escolhida pelo texto não é a única com ele (``None``: não conferido)."""

    by_html: int | None
    shown_elsewhere: int | None


#: Onde um passo roda: as repetições em volta dele, da mais externa à mais interna, cada
#: uma com o id do passo ``for_each`` e a posição do item.
type _IterationPath = tuple[tuple[str, int], ...]


class _Act(NamedTuple):
    """Uma ação que gravou numa linha escolhida pelo texto, para a rede contra repetições.

    ``order`` conta as ações da execução (a primeira é 1): as páginas visitadas numa
    volta guardam o número da última ação de antes da visita, e só as visitas de
    depois da ação (número igual ou maior) são páginas dela.
    """

    step_id: str
    key: str
    path: _IterationPath
    order: int = 0


def _iteration_depth(earlier: _IterationPath, current: _IterationPath) -> int | None:
    """Em que repetição em volta das duas ações elas estão em voltas diferentes.

    Devolve a posição dessa repetição em ``earlier`` (0 é a mais externa), ou ``None``
    quando as duas estão na mesma volta de todas as repetições em volta de ambas (ou
    fora delas): dois passos seguidos no mesmo item, ou duas repetições que não estão
    uma dentro da outra. Uma repetição só está num lugar do robô, então as que as duas
    têm em comum são o começo das duas listas. (Ids repetidos, que o portão de política
    recusa, só fariam duas repetições diferentes parecerem a mesma: na dúvida, recusa.)
    """
    for depth, ((loop, earlier_index), (other_loop, index)) in enumerate(
        zip(earlier, current, strict=False)
    ):
        if loop != other_loop:
            return None
        if earlier_index != index:
            return depth
    return None


def _other_iteration(earlier: _IterationPath, current: _IterationPath) -> tuple[str, int] | None:
    """A repetição em volta das duas ações em que elas estão em voltas diferentes.

    Devolve o id da repetição e a posição da ação anterior nela, ou ``None`` (ver
    :func:`_iteration_depth`).
    """
    depth = _iteration_depth(earlier, current)
    return None if depth is None else earlier[depth]


class _ListLoop:
    """O que cada item de uma repetição sobre uma entrada de lista faz no corpo dela.

    ``traces`` guarda, para cada item, os valores já montados dos passos que podem
    mudar a página ou a linha onde os seguintes agem (endereços, alvos, opções), ou
    ``None`` quando algum deles depende da página (variáveis, itens de uma tabela
    lida). ``actions`` guarda, para cada ação que grava numa linha escolhida pelo
    texto, os itens que chegam a ela, com o passo e os ramos que só a execução decide.
    """

    __slots__ = ("actions", "name", "traces")

    def __init__(self, name: str, indexes: Iterable[int]) -> None:
        self.name = name
        self.traces: dict[int, list[object] | None] = {index: [] for index in indexes}
        self.actions: dict[_RowAction, list[tuple[int, Step, _Undecided]]] = {}

    def forget(self, indexes: Iterable[int]) -> None:
        """Esses itens fazem algo que só a página decide: nunca contam como repetidos."""
        for index in indexes:
            self.traces[index] = None


class _Typed(NamedTuple):
    """Um campo em que o robô digitou um texto, para conferir antes de gravar.

    ``accepted`` é o valor que o campo tinha no fim do último passo do robô (o primeiro, o
    da digitação aceita: :meth:`Interpreter._refresh_typed`); ``intent`` e ``title`` vêm
    do robô (nunca valores), para a mensagem; ``frame`` é o do documento do campo, lido
    uma vez ao anotar.
    """

    handle: ElementHandle
    text: str
    accepted: str
    intent: str
    title: str
    #: O frame do documento do campo (para o vigia saber quais são do documento do alvo).
    frame: Frame | None


def _typed_value_matches(
    text: str, value: str, *, browser: str | None, number: bool = False
) -> bool:
    """Se o campo tem o texto que o robô digitou, como o navegador ou a página o guardam.

    Vale o próprio texto; num ``input`` ou ``textarea``, o valor que o navegador dá a ele
    (``browser``, de :data:`_TYPED_STATE_JS`: ``\\r\\n`` vira ``\\n``, um e-mail ou
    endereço perde os espaços das pontas); o texto com os brancos (e, num elemento
    editável, o espaço rígido) normalizados; e, num texto sem letras, a máscara da página:
    os
    mesmos dígitos, na mesma ordem (CPF, CNPJ, CEP, telefone, datas, centavos), com o sinal
    de menos que o texto tem (e sem um que ele não tem: :func:`_negative`); num número com casas decimais ou com o ponto dos milhares
    ("1.234,56", "10,5", "1234.56", "1.234"), o mesmo número ("1234,5" vira
    "R$ 1.234,50", mas "10,5" nunca vira "1,05", nem "1.234,00" vira "123.400,00"); e um
    inteiro pode ganhar uma ou duas casas decimais zeradas ("10" vira "10,00", "10.00" ou
    "10,00 €"; nunca "10,00 mil"). Um campo vazio nunca vale por um texto que não é branco
    (o navegador apaga o que um ``input[type=number]`` não aceita), e num
    ``input[type=number]`` (``number``) a regra dos dígitos não vale. Qualquer outra
    diferença não vale: o ``maxlength`` que cortou o texto, uma página que troca letras.
    """
    if text.strip() and not value.strip():
        return False
    if value == text or (browser is not None and value == browser):
        return True
    if value.split() == text.split():
        # Só os brancos mudaram (os espaços das pontas, dois espaços seguidos).
        return True
    if number or any(char.isalpha() for char in text):
        return False
    digits = _NOT_DIGIT.sub("", text)
    if not digits:
        return False
    if _negative(text) != _negative(value) or _NO_CENTS.search(value):
        # O sinal de menos que o texto tem precisa ficar, e o que ele não tem não aparece.
        # "10,-", "kr 10,-", "CHF 10.–": sem centavos (positivo) ou com o sinal no fim?
        return False
    decimal = _decimal_parts(text, typed=True)
    if decimal is not None:
        return _decimal_parts(value, typed=False) == decimal
    if _NOT_DIGIT.sub("", value) == digits:
        return True
    # Um inteiro que a página mostra com uma ou duas casas decimais zeradas ("10" vira
    # "10,00", "10.00" sem vírgula, "10,00 €"), com os mesmos dígitos ("0012" não vira
    # "12", nem "10" vira "10,000").
    whole, mark, fraction = value.rpartition(",")
    if not mark:
        whole, mark, fraction = value.rpartition(".")
        if "." in whole:
            return False
    return (
        text.strip().lstrip("-").isdigit()
        and bool(mark)
        and _ZERO_FRACTION.fullmatch(fraction) is not None
        and _ZERO_WHOLE.fullmatch(whole) is not None
        and _NOT_DIGIT.sub("", whole) == digits
    )


#: Do primeiro ao último dígito de um texto.
_DIGIT_SPAN = re.compile(r"[0-9](?:.*[0-9])?", re.DOTALL)
#: O sinal de menos: o hífen, o sinal de menos (U+2212), os traços e as outras formas do
#: hífen e do sinal de menos (sem quebra, de largura total, sobrescrito, subscrito).
_MINUS_SIGNS = (
    "-",
    "\u2212",
    "\u2013",
    "\u2010",
    "\u2011",
    "\u2012",
    "\u2014",
    "\u2015",
    "\ufe58",
    "\ufe63",
    "\uff0d",
    "\u2796",
    "\u02d7",
    "\u207b",
    "\u208b",
    "\u1806",
)
#: Antes das casas zeradas, só o sinal, um símbolo de moeda e os separadores de milhar
#: (nunca letras: "D 10.00", "Débito 10,00 €", "elfu 1.00").
_ZERO_WHOLE = re.compile(
    r"[\s\-\u2212\u2013]*(?:R\$|US\$|\$|€|£|CHF|BRL|USD|EUR)?[\s\-\u2212\u2013]*[0-9][0-9.\s'\u2019]*"
)
#: O traço logo depois de um separador decimal, no fim ("10,-", "CHF 10.–"): o preço sem
#: centavos (positivo) ou o sinal de menos depois do número? Na dúvida, não vale.
_NO_CENTS = re.compile(r"[0-9]\s*[.,]\s*[-\u2212\u2013\u2014][^0-9]*\Z")
#: Uma ou duas casas decimais zeradas, com brancos e, no máximo, um "%", "€" ou "$" depois
#: (nunca letras, parênteses ou um sinal: "10,00 mil", "(10,00)", "10,00-", "10,00 D").
_ZERO_FRACTION = re.compile(r"\s*0{1,2}\s*(?:%|€|\$)?\s*")


def _negative(text: str) -> bool:
    """Se o número do texto é negativo: com um sinal de menos antes do primeiro dígito
    ("-10", "R$ -10,00", "−10,00", e também "OS-12345"), entre parênteses ("(10,00)",
    "($10.00)") ou com o sinal de menos depois do último dígito ("10,00-")."""
    span = _DIGIT_SPAN.search(text)
    if span is None:
        return False
    before, after = text[: span.start()], text[span.end() :]
    if any(sign in before for sign in _MINUS_SIGNS):
        return True
    if before.count("(") > before.count(")") and after.count(")") > after.count("("):
        return True
    return after.rstrip().endswith(_MINUS_SIGNS)


#: Números como se digitam: com casas decimais ("1.234,56", "1234,56", "-10,5", "1234.56")
#: ou com o ponto dos milhares ("1.234").
_PT_DECIMAL = re.compile(r"-?(?:\d{1,3}(?:\.\d{3})+|\d+),\d+")
_DOT_DECIMAL = re.compile(r"-?\d+\.(?:\d{1,2}|\d{4,})")
_THOUSANDS = re.compile(r"-?\d{1,3}(?:\.\d{3})+")


def _decimal_parts(text: str, *, typed: bool) -> tuple[str, str] | None:
    """A parte inteira e a decimal de um número (só os dígitos, sem zeros que não contam).

    ``typed``: o texto que o robô digita, que só é lido como número num dos formatos de
    :data:`_PT_DECIMAL`, :data:`_DOT_DECIMAL` ou :data:`_THOUSANDS` (senão, ``None``: só
    dígitos, como num CPF ou num valor de centavos, seguem a regra dos dígitos). No valor
    do campo, a vírgula é o separador decimal; sem vírgula, o último ponto que não separa
    milhares; sem nenhum dos dois, o número é inteiro.
    """
    stripped = text.strip()
    if typed:
        if _PT_DECIMAL.fullmatch(stripped):
            whole, _, fraction = stripped.rpartition(",")
        elif _DOT_DECIMAL.fullmatch(stripped):
            whole, _, fraction = stripped.rpartition(".")
        elif _THOUSANDS.fullmatch(stripped):
            whole, fraction = stripped, ""
        else:
            return None
    elif "," in stripped:
        whole, _, fraction = stripped.rpartition(",")
    else:
        head, dot, tail = stripped.rpartition(".")
        tail_digits = _NOT_DIGIT.sub("", tail)
        if dot and len(tail_digits) != 3:
            whole, fraction = head, tail
        else:
            whole, fraction = stripped, ""
    return (
        _NOT_DIGIT.sub("", whole).lstrip("0"),
        _NOT_DIGIT.sub("", fraction).rstrip("0"),
    )


class _StepCtx:
    __slots__ = ("checked_at", "deadline", "element", "guard", "held", "locator", "step")

    def __init__(self, step: Step, deadline: float) -> None:
        self.step = step
        self.deadline = deadline
        #: O alvo encontrado por :meth:`Interpreter._resolve`, e quantas vezes a página
        #: principal tinha mudado quando ele foi conferido.
        self.locator: PwLocator | None = None
        self.checked_at = 0
        #: O elemento conferido, num passo que grava: a ação vai para ele, e não para o
        #: que o localizador encontrar na hora (ver :meth:`Interpreter._confirm_target`).
        self.element: ElementHandle | None = None
        #: O vigia posto na página logo antes de agir (:meth:`Interpreter._arm_guard`).
        self.guard: JSHandle | None = None
        #: Os campos digitados que a conferência antes de gravar leu com a referência
        #: deles (:meth:`Interpreter._typed_fields_hold`), quando ela foi feita.
        self.held: set[int] | None = None

    def remaining_ms(self) -> float:
        """Tempo que resta ao passo, em ms (nunca 0, que no Playwright é 'sem limite')."""
        loop = asyncio.get_running_loop()
        return max(1.0, (self.deadline - loop.time()) * 1000)


def _normalize_origin(url: str) -> str:
    """``esquema://host[:porta]`` em minúsculas, sem a porta padrão; vazio se não for web."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if parts.scheme not in _DEFAULT_PORTS or not host:
        return ""
    if port is None or port == _DEFAULT_PORTS[parts.scheme]:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _page_key(url: str) -> str:
    """A página de um endereço: domínio (e porta, se não for a padrão) e caminho normalizado.

    Na dúvida, a mesma página: a consulta, o fragmento e o esquema não contam
    (``/notas?pagina=2`` é ``/notas``, e ``http`` é ``https``), e o caminho vale
    decodificado, em minúsculas, sem parâmetros de trecho (``;jsessionid=...``), sem
    barras repetidas e sem a barra do fim (``/Notas/`` é ``/notas``).
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    origin = _normalize_origin(url)
    if origin:
        origin = origin.split("://", 1)[1]
    else:
        origin = f"{parts.scheme}:"
    segments = (segment.split(";", 1)[0] for segment in unquote(parts.path).split("/"))
    path = "/".join(segment for segment in segments if segment).casefold()
    return f"{origin}/{path}"


def _number_source(kind: FieldType, text: str) -> str:
    """O texto que vira número: sem separadores de milhar e com ponto decimal."""
    if kind is FieldType.INTEGER:
        return text.replace(".", "").replace(" ", "")
    number = text.replace(" ", "")
    if "," in number:
        number = number.replace(".", "").replace(",", ".")
    return number


def _number_texts(kind: FieldType, raw: str, converted: int | float) -> list[str]:
    """Textos em que um número convertido pode mostrar um segredo.

    Os dígitos de onde ele veio (um float arredonda o último dígito de um número
    longo) e as suas formas decimais exatas: ``str()`` de um float grande usa
    expoente (``1.2345678901234568e+16``) e esconderia os dígitos da conferência.
    """
    texts = [_number_source(kind, raw.strip()), str(converted)]
    if isinstance(converted, float):
        texts.append(format(Decimal(repr(converted)), "f"))
        if converted.is_integer():
            texts.append(str(int(converted)))
    return texts


def _coerce_output(kind: FieldType, value: Any) -> Any:
    """Converte o texto extraído no tipo declarado da saída, quando possível."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if kind is FieldType.INTEGER:
        digits = _number_source(kind, text)
        try:
            return int(digits)
        except ValueError:
            return value
    if kind is FieldType.NUMBER:
        number = _number_source(kind, text)
        try:
            converted = float(number)
        except ValueError:
            return value
        return converted if math.isfinite(converted) else value
    if kind is FieldType.BOOLEAN:
        folded = text.casefold()
        if folded in {"sim", "true", "verdadeiro", "1"}:
            return True
        if folded in {"não", "nao", "false", "falso", "0"}:
            return False
    return value


def _prepare_simple(spec: InputField, value: object, where: str, problems: list[str]) -> object:
    """Normaliza um valor simples de entrada e confere que ele pode ser digitado."""
    if spec.type is FieldType.INTEGER and isinstance(value, float):
        # JSON e formulários mandam 5 como 5.0; digitar "5.0" seria outro número
        # num portal em pt-BR. Um float grande demais já perdeu dígitos.
        if not (value.is_integer() and abs(value) <= _MAX_EXACT_FLOAT_INT):
            problems.append(f"a entrada '{where}' precisa ser um número inteiro")
            return value
        value = int(value)
    try:
        templates.format_value(value)
    except templates.TemplateError as exc:
        problems.append(f"a entrada '{where}' não pode ser digitada: {exc}")
    return value


def _url_slots(parts: list[templates.Part]) -> dict[int, _UrlSlot]:
    """Em que trecho do endereço cai cada referência, pela posição dela em ``parts``.

    Os valores são codificados e nunca trazem ``/``, ``?`` nem ``#``: só os trechos
    literais antes de cada referência decidem onde ela cai. Sem ``//`` no começo,
    vale a regra mais estrita (caminho). No fragmento, até um ``?`` dentro dele,
    vale a mesma regra do caminho (rota): aplicações de página única escolhem a
    tela por ele e muitas decodificam ``%2F`` antes (o AngularJS em modo
    ``#!``, por exemplo).
    """
    slots: dict[int, _UrlSlot] = {}
    before = ""
    for index, part in enumerate(parts):
        if isinstance(part, str):
            before += part
            continue
        if (hash_at := before.find("#")) != -1:
            slots[index] = "fragmento" if "?" in before[hash_at:] else "rota"
        elif "?" in before:
            slots[index] = "consulta"
        elif (start := before.find("//")) != -1 and not any(
            separator in before[start + 2 :] for separator in "/\\"
        ):
            slots[index] = "autoridade"
        else:
            slots[index] = "caminho"
        before += "x"  # o valor ocupa o lugar, mas não muda o trecho
    return slots


def _path_value_problem(text: str) -> str | None:
    """Por que um valor não pode ser um trecho do caminho do endereço (``None`` se pode).

    Codificar não basta: muitos servidores (ASGI, WSGI, Laravel...) decodificam o
    caminho antes de escolher a rota, e ``%2F`` volta a ser uma barra; os que
    decodificam duas vezes fazem o mesmo com ``%252F``, por isso ``%`` também é
    recusado. Um valor vazio some e junta os trechos vizinhos
    (``/notas//cancelar``), e ``.`` e ``..`` mudam de pasta mesmo codificados. A
    conferência vale também para a forma NFKC do valor, que alguns servidores
    aplicam: a barra larga ``／`` vira ``/`` e ``‥`` vira ``..``.
    """
    if not text:
        return "está vazio"
    for form in (text, unicodedata.normalize("NFKC", text)):
        if form in (".", ".."):
            return f"é '{form}' (ou equivale a isso), que mudaria de pasta"
        if "/" in form or "\\" in form:
            return "contém '/' ou '\\' (ou um equivalente), que mudariam o caminho"
        if "%" in form:
            return "contém '%' (ou um equivalente), que um servidor poderia decodificar de novo"
    return None


def _encode_url_value(text: str, slot: _UrlSlot) -> str:
    """Codifica um valor como um único trecho do endereço.

    No caminho e na rota do fragmento, recusa os valores que mudariam a rota
    mesmo codificados.
    """
    if (where := _PATH_SLOTS.get(slot)) is not None and (
        problem := _path_value_problem(text)
    ) is not None:
        raise templates.TemplateError(f"não pode ir {where}: {problem}")
    return quote(text, safe="")


def _normalize_text(text: str) -> str:
    """O texto como o Playwright o compara.

    Sem U+200B e U+00AD, sem brancos do JavaScript nas pontas (U+FEFF inclusive) e
    com cada sequência deles virando um espaço. Uma chave que fica vazia assim
    escolheria qualquer elemento sem texto (uma célula vazia, um botão só com ícone).
    """
    return _JS_SPACES.sub(" ", _IGNORED_IN_TEXT.sub("", text)).strip(" ")


def _row_key_value(_index: int, text: str) -> str:
    """Confere um valor do texto que escolhe o contêiner (``encode`` de ``render``).

    Um valor que fica vazio depois de normalizado nunca pode entrar: com
    ``Nota {{ item.numero }}`` e o número vazio (ou só um BOM), sobraria
    ``Nota``, que pode ser o texto inteiro de um rótulo dentro de outra linha.
    """
    if not _normalize_text(text):
        raise templates.TemplateError(
            "ficaria vazio no texto que escolhe o contêiner, e sem ele não há como saber qual usar"
        )
    return text


def _js_escape(char: str) -> str:
    # Letras e dígitos ASCII vão como estão; o resto do ASCII vira \xHH, que vale
    # igual no Python e no JavaScript e nunca é aspas, barra ou ">>" para o seletor.
    if char.isascii() and not char.isalnum():
        return f"\\x{ord(char):02x}"
    return char


def _whole_text(key: str) -> re.Pattern[str]:
    """Expressão, avaliada no navegador, do texto inteiro de um elemento igual a ``key``.

    ``key`` já vem normalizada. O Playwright testa expressões no texto cru
    (``textContent``), então a expressão aceita o que ele ignora ao comparar
    textos: brancos nas pontas e repetidos, U+200B e U+00AD. Os caracteres
    precisam ser os mesmos, com as mesmas maiúsculas e minúsculas: ``ab3x`` nunca
    escolhe a linha de ``AB3X``. O ``text-transform`` do CSS não conta, porque o
    texto é o do HTML (o gravador também grava a chave a partir dele); o texto
    mostrado é conferido à parte, para nenhuma outra linha mostrar a mesma chave
    (:meth:`Interpreter._row_issue`).
    """
    words = (_INSIDE_WORD.join(_js_escape(char) for char in word) for word in key.split(" "))
    body = _BETWEEN_WORDS.join(words)
    return re.compile(f"^{_TEXT_EDGE}{body}{_TEXT_EDGE}$")


def _step_targets(step: Step) -> list[Target]:
    """Alvos de um passo, inclusive o da condição de ``if`` e ``assert``."""
    targets: list[Target] = []
    for holder in (step, getattr(step, "condition", None)):
        target = getattr(holder, "target", None)
        if isinstance(target, Target):
            targets.append(target)
    return targets


def _target_signature(kind: str, target: Target, render: Callable[[str], str]) -> _RowAction:
    """A ação de um passo num alvo, na conferência antes do primeiro passo.

    É o tipo do passo e onde ele age: o localizador principal (o único que passos
    que gravam usam), o contêiner e os quadros, com os valores já montados. O id do
    passo e a descrição do alvo não entram: dois passos que agem no mesmo elemento,
    nos dois ramos de um ``if``, fazem a mesma ação. (Na execução, a rede contra
    ações repetidas não olha o alvo: :meth:`Interpreter._row_not_acted_on`.)
    """

    def locator(spec: Locator) -> tuple[object, ...]:
        name = render(spec.name) if spec.name is not None else None
        return (spec.by.value, render(spec.value), name, spec.exact)

    within = locator(target.within) if target.within is not None else None
    return (kind, tuple(target.frames), within, locator(target.locators[0]))


def _trace_templates(step: Step) -> list[str]:
    """Os templates que decidem onde um passo age ou para onde ele leva a página.

    Inclui os localizadores alternativos: um passo de leitura pode usá-los.
    """
    found: list[str] = []
    if isinstance(step, NavigateStep):
        found.append(step.url)
    target = getattr(step, "target", None)
    if isinstance(target, Target):
        for spec in (*target.locators, target.within):
            if spec is not None:
                found.extend(t for t in (spec.value, spec.name) if t is not None)
        if target.within_has_text is not None:
            found.append(target.within_has_text)
    if isinstance(step, SelectStep):
        found.append(step.option)
    return found


def _page_trace(
    step: Step, scopes: Mapping[templates.Scope, Mapping[str, object]]
) -> tuple[object, ...]:
    """O que um passo faz com a página, com os valores já montados (ver :class:`_ListLoop`).

    Num ``navigate``, a página do endereço, com cada valor codificado como na
    execução (como a rede contra ações repetidas a compara); nos outros, a ação no
    alvo, a linha e o que pode mudar a página (a opção escolhida, a tecla). O valor
    digitado não entra: digitar outro texto no mesmo campo não leva a outra página.
    Levanta :class:`~rapa.core.templates.TemplateError` se um valor faltar.
    """

    def render(template: str) -> str:
        return templates.render(template, scopes)

    if isinstance(step, NavigateStep):
        url = templates.render(step.url, scopes, encode=lambda _index, text: quote(text, safe=""))
        return ("navigate", _page_key(url))
    target = getattr(step, "target", None)
    if not isinstance(target, Target):
        return (step.kind, getattr(step, "key", None))
    row = None
    if target.within_has_text is not None:
        row = _normalize_text(
            templates.render(target.within_has_text, scopes, encode=_row_key_value)
        )
    extra: object = None
    if isinstance(step, SelectStep):
        extra = render(step.option)
    elif isinstance(step, PressStep):
        extra = step.key
    elif isinstance(step, CheckStep):
        extra = step.checked
    elif isinstance(step, UploadStep):
        extra = step.file_input
    return (_target_signature(step.kind, target, render), row, extra)


def _changes_page(steps: Iterable[Step]) -> bool:
    """Se algum dos passos, inclusive os de dentro de contêineres, pode mudar a página."""
    for step in steps:
        if isinstance(step, ForEachStep):
            if _changes_page(step.steps):
                return True
        elif isinstance(step, IfStep):
            if _changes_page(step.then) or _changes_page(step.otherwise):
                return True
        elif not isinstance(step, _PAGE_READS):
            return True
    return False


def _join_names(names: list[str]) -> str:
    """``'a'``, ``'a' e 'b'``, ``'a', 'b' e 'c'``."""
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} e {names[-1]}"


def _repeated_rows(loop: _ListLoop) -> list[str]:
    """Itens de uma lista de entrada que repetiriam uma ação na mesma linha.

    São os que chegam à mesma ação que grava, com o mesmo texto normalizado para
    escolher a linha, e que fazem no resto da repetição os mesmos passos com os
    mesmos valores onde isso pode mudar a página: na execução, a segunda vez
    pararia no meio, depois de outros itens já terem gravado. Numa repetição com
    outras dentro, as ações de dentro contam para cada item de fora: dois meses que
    abrem a mesma página (só a consulta muda) cancelariam de novo as mesmas notas.
    """
    found: list[str] = []
    for uses in loop.actions.values():
        groups: dict[tuple[object, ...], list[tuple[int, Step, _Undecided]]] = {}
        for use in uses:
            trace = loop.traces.get(use[0])
            if trace is not None:
                groups.setdefault(tuple(trace), []).append(use)
        for group in groups.values():
            indexes = sorted({index for index, _, _ in group})
            if len(indexes) < 2:
                continue
            items = _join_names([f"'{loop.name}[{index}]'" for index in indexes])
            step_ids = [f"'{step_id}'" for step_id in dict.fromkeys(s.id for _, s, _ in group)]
            if len(step_ids) == 1:
                steps = f"o passo {step_ids[0]} agir"
            else:
                steps = f"os passos {_join_names(step_ids)} (com o mesmo alvo) agirem"
            target = getattr(group[0][1], "target", None)
            intent = target.intent if isinstance(target, Target) else group[0][1].title
            undecided = next((u for _, _, u in group if u), ())
            found.append(
                f"os itens {items} escolheriam, pelo mesmo texto, a mesma linha para {steps} "
                f"em '{intent}', e a mesma ação se repetiria nela (a lista tem itens "
                "repetidos, ou itens que levam à mesma página?)"
                f"{_undecided_reason(undecided, 'a lista é recusada')}"
            )
    return found


def _static_holds(
    condition: Condition, values: Mapping[str, Any], item: Mapping[str, object] | None
) -> bool | None:
    """O resultado de uma condição que dá para saber antes do primeiro passo.

    Só um ``equals`` cujos dois lados usam apenas entradas e, numa repetição sobre
    uma entrada de lista, o item: ele é avaliado como seria durante a execução.
    ``None`` quando só a execução sabe (a página, variáveis, um template que falha).
    """
    if not isinstance(condition, EqualsCondition):
        return None
    scopes: dict[templates.Scope, Mapping[str, object]] = {templates.Scope.INPUTS: values}
    if item is not None:
        scopes[templates.Scope.ITEM] = item
    try:
        left = templates.render(condition.left, scopes)
        right = templates.render(condition.right, scopes)
    except templates.TemplateError:
        return None  # variáveis só existem durante a execução; o resto falha lá
    return (left == right) != condition.negate


def _undecided_reason(undecided: _Undecided, refused: str = "o valor é recusado") -> str:
    """Por que o valor de um passo que talvez nem rode é conferido antes do primeiro passo."""
    if not undecided:
        return ""
    branches = "esse ramo roda" if len(undecided) == 1 else "esses ramos rodam"
    return (
        f" (o passo fica {' e '.join(undecided)}, e só durante a execução se sabe se "
        f"{branches}; na dúvida, {refused} antes do primeiro passo)"
    )


def _has_references(template: str) -> bool:
    try:
        return templates.has_references(template)
    except templates.TemplateError:
        return False  # o template inválido é recusado ao ser montado


def _describe_condition(condition: Condition) -> str:
    match condition:
        case VisibleCondition(target=target, negate=negate):
            text = f"'{target.intent}' visível"
        case TextContainsCondition(target=target, text=needle, negate=negate):
            where = f"em '{target.intent}'" if target is not None else "na página"
            text = f"texto '{needle}' {where}"
        case UrlContainsCondition(value=needle, negate=negate):
            text = f"endereço contendo '{needle}'"
        case EqualsCondition(left=left, right=right, negate=negate):
            text = f"'{left}' igual a '{right}'"
        case _:  # pragma: no cover - conjunto fechado
            assert_never(condition)
    return f"não ({text})" if negate else text


class Interpreter:
    """Executa um manifesto num contexto de navegador novo.

    Cada instância executa uma única vez. O chamador cria e fecha o contexto
    (ver :func:`rapa.runner.browser.run_context`); o interpretador abre nele a
    sua página e recusa contextos já usados por outra execução.
    """

    def __init__(
        self,
        manifest: Manifest,
        inputs: Mapping[str, Any],
        *,
        secrets: SecretProvider,
        files: FileProvider,
        artifacts: ArtifactSink,
        events: EventSink,
        run_id: UUID,
        attempt: int,
        first_seq: int = 0,
        mode: RunMode = RunMode.FULL,
        deadline: datetime | None = None,
        cancel: asyncio.Event | None = None,
        screenshot_every_step: bool = False,
        block_other_domains: bool = True,
        redact_values: Iterable[str] = (),
        postcondition_timeout_ms: int = 5_000,
        heal_after_ms: int = 1_000,
        poll_interval_ms: int = 100,
    ) -> None:
        if attempt < 1:
            raise ValueError("attempt começa em 1")
        if first_seq < 0:
            raise ValueError("first_seq não pode ser negativo")
        self._manifest = manifest
        self._raw_inputs = dict(inputs)
        self._secrets = secrets
        self._files = files
        self._artifact_sink = artifacts
        self._events = events
        self._run_id = run_id
        self._attempt = attempt
        self._mode = mode
        if deadline is not None and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        self._deadline_at = deadline
        self._cancel = cancel
        self._screenshot_every_step = screenshot_every_step
        self._block_other_domains = block_other_domains
        self._redactor = _Redactor(redact_values)
        self._postcondition_timeout_s = postcondition_timeout_ms / 1000
        self._heal_after_s = heal_after_ms / 1000
        self._poll_s = poll_interval_ms / 1000

        self._credentials: dict[str, CredentialRef] = {c.name: c for c in manifest.credentials}
        self._allowed_hosts = frozenset(manifest.allowed_domains)
        steps = [step for step, _ in manifest.iter_steps()]
        step_ids = frozenset(step.id for step in steps)
        self._event_constants: dict[str, frozenset[str]] = {
            **_EVENT_CONSTANTS,
            **dict.fromkeys(_STEP_ID_KEYS, step_ids),
            "tipo": frozenset(step.kind for step in steps) | {kind.value for kind in ArtifactKind},
        }
        # A numeração dos eventos é da execução: cada tentativa continua a anterior.
        self._seq = first_seq
        self._used = False
        self._page: Page | None = None
        self._deadline = math.inf
        self._started_at = 0.0
        self._inputs: dict[str, Any] = {}
        self._vars: dict[str, Any] = {}
        #: Itens das repetições em andamento: o item, a posição dele e o id da repetição.
        self._items: list[tuple[Mapping[str, object], int, str]] = []
        #: Efeitos dos contêineres em andamento (ver :meth:`_effect`).
        self._block_effects: list[Effect] = []
        self._steps_done = 0
        self._irreversible_step: Step | None = None
        #: Ações que gravaram numa linha dentro de repetições, pelo texto normalizado
        #: da linha (ver :meth:`_row_not_acted_on`).
        self._acts: dict[str, list[_Act]] = {}
        #: Quantas ações já foram anotadas (o ``order`` da última).
        self._order = 0
        #: Páginas visitadas em cada volta de repetição em andamento desde a primeira ação
        #: dela numa linha (a volta é o caminho de :data:`_IterationPath` até ela): a
        #: página e o ``order`` da última ação de antes da última visita.
        self._visits: dict[_IterationPath, dict[str, int]] = {}
        #: Páginas por onde a página principal passou fora do passo ``navigate`` do robô,
        #: cada uma com o ``order`` da última ação de antes da última passagem: valem para
        #: as voltas que já acabaram, até o fim da repetição delas (a resposta de um
        #: formulário, um temporizador ou um popup ainda podem chegar). Um mapa só para
        #: a execução inteira, e não um por volta: o custo é linear no número de voltas.
        self._navigated: dict[str, int] = {}
        #: Quantas vezes a página principal mudou de documento ou de endereço (ver
        #: :meth:`_confirm_target`).
        self._main_frame_changes = 0
        #: Os campos de texto que o robô digitou e que ainda estão na página
        #: (:meth:`_typed_fields_hold`, :meth:`_refresh_typed`).
        self._typed: list[_Typed] = []
        #: A página (:func:`_page_key`) que o passo ``navigate`` em andamento abre, e os
        #: pedidos dele: o primeiro para essa página e os redirecionamentos desse.
        self._robot_navigation: str | None = None
        self._robot_requests: list[Request] = []
        self._artifacts: list[ArtifactRef] = []
        self._secret_locators: list[PwLocator] = []
        self._blocked_navigation: str | None = None
        self._screenshots = 0

    @classmethod
    def from_job(
        cls,
        job: RunnerJob,
        *,
        secrets: SecretProvider,
        files: FileProvider,
        artifacts: ArtifactSink,
        events: EventSink,
        **options: Any,
    ) -> Self:
        """Monta o interpretador a partir do trabalho entregue pelo plano de controle."""
        return cls(
            job.manifest,
            job.inputs,
            secrets=secrets,
            files=files,
            artifacts=artifacts,
            events=events,
            run_id=job.run_id,
            attempt=job.attempt,
            first_seq=job.first_seq,
            mode=job.mode,
            deadline=job.deadline,
            **options,
        )

    @property
    def irreversible_started(self) -> bool:
        return self._irreversible_step is not None

    # ------------------------------------------------------------------
    # Execução
    # ------------------------------------------------------------------

    async def run(self, context: BrowserContext) -> RunResult:
        """Executa o robô e devolve o resultado. Só levanta exceção se os
        eventos não puderem ser registrados ou por uso indevido da API."""
        self._claim(context)
        loop = asyncio.get_running_loop()
        self._started_at = loop.time()
        self._deadline = self._loop_deadline()
        await self._emit_run_started()

        state = RunState.SUCCEEDED
        code: str | None = None
        message: str | None = None
        failed_step: str | None = None
        try:
            if loop.time() >= self._deadline:
                raise StepError(
                    ErrorCode.TEMPO_ESGOTADO, "O prazo da execução acabou antes de começar."
                )
            self._inputs = self._prepare_inputs()
            self._page = await self._open_page(context)
            await self._run_block(self._manifest.steps)
            await self._check_postconditions()
        except _StopBeforeWrite as stop:
            await self._emit(
                RunEventKind.RUN_STOPPED_BEFORE_WRITE,
                stop.step.id,
                {"titulo": stop.step.title, "efeito": stop.step.effect.value},
            )
            code = ErrorCode.PAROU_ANTES_DE_GRAVAR
            message = (
                f"Validação segura: a execução parou antes do passo '{stop.step.title}', "
                "o primeiro que grava no sistema de destino."
            )
        except _Cancelled as cancelled:
            state = RunState.CANCELLED
            code = ErrorCode.CANCELADA
            message = f"A execução foi cancelada antes do passo '{cancelled.step.title}'."
            failed_step = cancelled.step.id
        except StepError as err:
            if not err.reported:
                err.reported = True
                await self._screenshot(err.step_id, "falha")
            state = RunState.FAILED
            code = err.code
            message = err.message
            failed_step = err.step_id

        if state is not RunState.SUCCEEDED and self._irreversible_step is not None:
            state = RunState.INDETERMINATE
            message = (
                f"Resultado indeterminado: o passo irreversível '{self._irreversible_step.title}' "
                f"({self._irreversible_step.id}) já tinha começado quando a execução parou. "
                f"Confira no sistema de destino antes de repetir. Motivo: {message}"
            )
        return await self._finish(state, code, message, failed_step)

    async def fail_without_browser(self, code: ErrorCode, message: str) -> RunResult:
        """Registra uma execução que nem chegou a abrir o navegador."""
        if self._used:
            raise RuntimeError("este interpretador já executou")
        self._used = True
        loop = asyncio.get_running_loop()
        self._started_at = loop.time()
        await self._emit_run_started()
        return await self._finish(RunState.FAILED, code, message, None)

    async def _open_page(self, context: BrowserContext) -> Page:
        try:
            if self._block_other_domains:
                await context.route("**/*", self._guard_request)
            # O vigia dos passos que gravam decide antes dos ouvintes da própria página.
            await context.add_init_script(script=_GUARD_HOOK_JS)
            page = await context.new_page()
        except PlaywrightError as exc:
            raise StepError(
                ErrorCode.NAVEGADOR_INDISPONIVEL,
                "Não consegui abrir uma página no navegador.",
                details={"detalhe": self._detail(exc)},
            ) from None
        # Síncronos: quando page.url muda, a página já foi anotada nas voltas em aberto.
        page.on("request", self._on_request)
        page.on("framenavigated", self._on_frame_navigated)
        return page

    def _iteration_path(self) -> _IterationPath:
        """As repetições em volta do passo atual, cada uma com a posição do item."""
        return tuple((loop, index) for _, index, loop in self._items)

    def _open_iterations(self) -> list[_IterationPath]:
        """As voltas em andamento que já agiram numa linha, de fora para dentro.

        As que já acabaram contam as navegações pelo mapa da execução inteira
        (:meth:`_navigation`, :meth:`_row_not_acted_on`).
        """
        path = self._iteration_path()
        prefixes = (path[:depth] for depth in range(1, len(path) + 1))
        return [prefix for prefix in prefixes if prefix in self._visits]

    def _visit(self, url: str) -> None:
        """Anota a página de ``url`` (ver :func:`_page_key`) nas voltas em andamento."""
        page = _page_key(url)
        for iteration in self._open_iterations():
            self._visits[iteration][page] = self._order

    def _navigation(self, url: str, *, robot: bool) -> None:
        """A página principal passou por ``url``: conta nas voltas em andamento e, fora do
        passo ``navigate`` do robô (``robot``), também nas que já acabaram."""
        self._visit(url)
        if not robot:
            self._navigated[_page_key(url)] = self._order

    def _on_request(self, request: Request) -> None:
        """Uma navegação da página principal conta nas voltas em aberto.

        O endereço pedido e cada um dos redirecionamentos viram páginas das ações
        dessas voltas, e (fora do ``navigate`` do robô) também das voltas que já
        acabaram (:meth:`_navigation`). A navegação do próprio passo ``navigate`` do
        robô (o primeiro pedido para a página dele e os redirecionamentos desse) não
        conta nas voltas que já acabaram: a página que o robô abre antes de agir é
        outra página. Qualquer outra, mesmo durante o ``navigate`` (um popup que leva
        a página a outro endereço), conta.
        """
        try:
            if not request.is_navigation_request() or request.frame.parent_frame is not None:
                return
        except PlaywrightError:  # pedidos de service worker não têm quadro
            return
        origin = request.redirected_from
        robot = self._robot_navigation is not None and (
            origin in self._robot_requests
            if origin is not None
            else not self._robot_requests and _page_key(request.url) == self._robot_navigation
        )
        if robot:
            self._robot_requests.append(request)
        self._navigation(request.url, robot=robot)

    def _on_frame_navigated(self, frame: Frame) -> None:
        """A página principal mudou de endereço (inclusive sem pedido, por ``pushState``).

        Onde o passo ``navigate`` do robô para não conta nas voltas que já acabaram
        (:meth:`_on_request`); qualquer outro endereço conta. Os campos digitados até aqui
        continuam conferidos antes de gravar enquanto o documento deles existe (uma
        mudança de endereço no mesmo documento, por ``pushState`` ou ``#``, não os
        tira): o campo de um documento que acabou sai da lista na conferência seguinte
        (:meth:`_typed_fields_hold`, :meth:`_refresh_typed`).
        """
        if frame.parent_frame is None:
            self._main_frame_changes += 1
            robot = self._robot_navigation is not None and _page_key(frame.url) in {
                self._robot_navigation,
                *(_page_key(request.url) for request in self._robot_requests),
            }
            self._navigation(frame.url, robot=robot)

    def _record_act(self, act: _Act) -> _Act:
        """Anota uma ação numa linha, antes de agir, na página de agora.

        Daqui em diante, as páginas visitadas também são páginas dela, para as voltas
        seguintes de cada repetição em volta dela (depois do fim da volta, menos as
        que o passo ``navigate`` do robô abre: :meth:`_on_request`).
        """
        self._order += 1
        act = act._replace(order=self._order)
        self._acts.setdefault(act.key, []).append(act)
        for depth in range(1, len(act.path) + 1):
            self._visits.setdefault(act.path[:depth], {})
        # Só nas voltas dela: a página onde ela age não é uma navegação.
        self._visit(self.page.url)
        return act

    def _claim(self, context: BrowserContext) -> None:
        if self._used:
            raise RuntimeError("cada Interpreter executa uma única vez; crie outro")
        if context in _claimed_contexts:
            raise RuntimeError(
                "este contexto do navegador já foi usado por outra execução; crie um novo"
            )
        self._used = True
        _claimed_contexts.add(context)

    def _loop_deadline(self) -> float:
        if self._deadline_at is None:
            return math.inf
        remaining = (self._deadline_at - datetime.now(UTC)).total_seconds()
        return asyncio.get_running_loop().time() + remaining

    async def _emit_run_started(self) -> None:
        await self._emit(
            RunEventKind.RUN_STARTED,
            None,
            {
                "robo": self._manifest.name,
                "fingerprint": self._manifest.fingerprint(),
                "modo": self._mode.value,
                "passos": sum(1 for _ in self._manifest.iter_steps()),
            },
        )

    async def _finish(
        self, state: RunState, code: str | None, message: str | None, failed_step: str | None
    ) -> RunResult:
        loop = asyncio.get_running_loop()
        message = self._redactor.text(message) if message is not None else None
        outputs = (
            self._collect_outputs()
            if state in (RunState.SUCCEEDED, RunState.INDETERMINATE)
            else None
        )
        data: dict[str, Any] = {
            "estado": state.value,
            "passos_concluidos": self._steps_done,
            "duracao_ms": round((loop.time() - self._started_at) * 1000),
            "irreversivel_iniciado": self.irreversible_started,
        }
        if code is not None:
            data["codigo"] = str(code)
        if message is not None:
            data["mensagem"] = message
        if failed_step is not None:
            data["passo"] = failed_step
        if self._irreversible_step is not None:
            data["passo_irreversivel"] = self._irreversible_step.id
        kind = (
            RunEventKind.RUN_SUCCEEDED if state is RunState.SUCCEEDED else RunEventKind.RUN_FAILED
        )
        await self._emit(kind, failed_step, data)
        return RunResult(
            run_id=self._run_id,
            attempt=self._attempt,
            state=state,
            outputs=outputs,
            error_code=str(code) if code is not None else None,
            error_message=message,
            failed_step_id=failed_step,
            irreversible_started=self.irreversible_started,
            steps_done=self._steps_done,
            artifacts=list(self._artifacts),
        )

    def _prepare_inputs(self) -> dict[str, Any]:
        """Confere e normaliza as entradas antes do primeiro passo.

        O que faria a execução falhar no meio por causa das entradas (campo de
        item ausente, número que não pode ser digitado, lista maior que a
        repetição aceita, valor que não pode ir no caminho ou na rota de um
        endereço, valor vazio no texto que escolhe um contêiner) falha aqui, antes
        de qualquer passo gravar. As entradas recebidas não são alteradas.
        """
        values = dict(self._raw_inputs)
        problems: list[str] = []
        for spec in self._manifest.inputs:
            value = values.get(spec.name)
            if value is None:
                if spec.required:
                    problems.append(f"entrada obrigatória ausente: {spec.name}")
                elif spec.type is FieldType.LIST:
                    values[spec.name] = []
                elif spec.type is FieldType.FILE:
                    values.pop(spec.name, None)
                else:
                    values[spec.name] = ""
                continue
            if spec.type is FieldType.LIST:
                if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
                    values[spec.name] = self._prepare_items(spec, value, problems)
                else:
                    problems.append(f"a entrada '{spec.name}' precisa ser uma lista de objetos")
            elif spec.type is FieldType.FILE:
                if not isinstance(value, str):
                    problems.append(
                        f"a entrada '{spec.name}' precisa ser o identificador do arquivo"
                    )
            else:
                values[spec.name] = _prepare_simple(spec, value, spec.name, problems)
        self._check_input_uses(values, problems)
        if problems:
            shown = problems[:_MAX_PROBLEMS]
            if len(problems) > _MAX_PROBLEMS:
                shown.append(f"e mais {len(problems) - _MAX_PROBLEMS} problemas")
            raise StepError(
                ErrorCode.ENTRADA_INVALIDA,
                "Entradas inválidas: " + "; ".join(shown) + ".",
                details={"problemas": shown},
            )
        self._check_list_limits(values)
        return values

    @staticmethod
    def _prepare_items(
        spec: InputField, items: list[Mapping[str, object]], problems: list[str]
    ) -> list[dict[str, object]]:
        """Itens de uma lista com os mesmos cuidados das entradas de cima."""
        prepared: list[dict[str, object]] = []
        for index, item in enumerate(items):
            row = dict(item)
            for field in spec.item_fields or ():
                where = f"{spec.name}[{index}].{field.name}"
                current = row.get(field.name)
                if current is None:
                    if field.required:
                        problems.append(f"campo obrigatório ausente: {where}")
                    else:
                        # Campo opcional sem valor é digitado vazio (campos de item
                        # nunca são listas nem arquivos).
                        row[field.name] = ""
                    continue
                row[field.name] = _prepare_simple(field, current, where, problems)
            prepared.append(row)
        return prepared

    def _check_input_uses(self, values: Mapping[str, Any], problems: list[str]) -> None:
        """Confere os usos que só dependem das entradas e dos itens de listas de entrada.

        São os valores que vão no caminho ou na rota (depois do ``#``) de um
        endereço e os que vão no texto que escolhe um contêiner. Se um deles não
        serve, a execução pararia nesse passo, talvez depois de outros já terem
        gravado. Variáveis só existem durante a execução: essas são conferidas
        quando o passo roda.

        Só contam os passos que podem rodar. Um ``if`` cuja condição dá para
        avaliar antes (:func:`_static_holds`) só tem conferido o ramo que vai
        rodar (numa repetição sobre uma entrada de lista, item a item), e uma
        repetição sobre uma lista de entrada vazia não roda. Quando a condição só
        se conhece durante a execução, os dois ramos são conferidos, e a mensagem
        diz em que ramo está o passo.

        Também recusa os itens repetidos de uma lista de entrada
        (:func:`_repeated_rows`): na execução, a rede contra ações repetidas pararia
        no segundo deles, talvez depois de outros itens já terem gravado. Listas
        lidas da página (variáveis) ficam com a rede, durante a execução.
        """
        found: list[str] = []

        def walk(
            steps: Iterable[Step],
            rows: _Rows | None,
            undecided: _Undecided,
            loops: tuple[tuple[_ListLoop, tuple[int, ...]], ...],
            inherited: Effect,
        ) -> None:
            # ``inherited``: o efeito dos contêineres em volta (:func:`_effective_effect`).
            for step in steps:
                effect = _effective_effect(step, (inherited,))
                if isinstance(step, NavigateStep):
                    found.extend(self._url_value_problems(step, values, rows, undecided))
                for target in _step_targets(step):
                    found.extend(self._row_key_problems(step, target, values, rows, undecided))
                if loops and rows is not None:
                    self._trace_step(step, values, rows, undecided, loops, effect)
                if isinstance(step, ForEachStep):
                    inner = self._loop_rows(step, values)
                    if inner is None:
                        # Sobre uma tabela lida: quantas vezes e onde ela age, só a página
                        # sabe. Se ela pode mudar a página, nenhum item de fora conta.
                        if _changes_page(step.steps):
                            for each_loop, each_reach in loops:
                                each_loop.forget(each_reach)
                        walk(step.steps, None, undecided, (), effect)
                    elif inner[1]:  # sobre uma lista de entrada vazia, nada roda
                        indexes = tuple(index for index, _ in inner[1])
                        loop = _ListLoop(inner[0], indexes)
                        walk(step.steps, inner, undecided, (*loops, (loop, indexes)), effect)
                        found.extend(_repeated_rows(loop))
                elif isinstance(step, IfStep):
                    for block, block_rows, block_undecided in self._branches(
                        step, values, rows, undecided
                    ):
                        block_loops = loops
                        if rows is not None and block_rows is not None:
                            # Só os itens que chegam ao ramo (a repetição de dentro é a deles).
                            reach = tuple(index for index, _ in block_rows[1])
                            block_loops = (*loops[:-1], (loops[-1][0], reach))
                        walk(block, block_rows, block_undecided, block_loops, effect)

        walk(self._manifest.steps, None, (), (), Effect.READ)
        for condition in self._manifest.postconditions:
            target = getattr(condition, "target", None)
            if isinstance(target, Target):
                found.extend(self._row_key_problems(None, target, values, None, ()))
        seen = set(problems)
        for problem in found:
            if problem not in seen:  # a mesma entrada pode aparecer em vários passos
                seen.add(problem)
                problems.append(problem)

    @staticmethod
    def _loop_rows(step: ForEachStep, values: Mapping[str, Any]) -> _Rows | None:
        """Os itens de uma repetição, quando ela percorre uma entrada de lista."""
        scope, name = step.items.split(".", 1)
        rows = values.get(name) if scope == "inputs" else None
        if isinstance(rows, list) and all(isinstance(row, Mapping) for row in rows):
            return name, list(enumerate(rows))
        return None

    @staticmethod
    def _trace_step(
        step: Step,
        values: Mapping[str, Any],
        rows: _Rows,
        undecided: _Undecided,
        loops: tuple[tuple[_ListLoop, tuple[int, ...]], ...],
        effect: Effect,
    ) -> None:
        """Anota o que ``step`` faz para cada item das repetições sobre listas de entrada.

        ``loops`` são essas repetições em volta do passo, da mais externa à mais
        interna, cada uma com os itens que chegam ao passo; ``rows`` são os itens da
        mais interna, de onde vem ``item``; ``effect`` é o efeito com que o passo
        roda (:func:`_effective_effect`). Ver :class:`_ListLoop`.
        """
        if isinstance(step, (ForEachStep, IfStep)):
            return  # nos contêineres, contam os passos de dentro
        reads_page = isinstance(step, _PAGE_READS)
        target = getattr(step, "target", None)
        acts_on_row = (
            effect is not Effect.READ
            and isinstance(target, Target)
            and target.within is not None
            and target.within_has_text is not None
        )
        if reads_page and not acts_on_row:
            return  # só lê: não muda a página nem a linha dos passos seguintes
        refs: list[templates.Ref] = []
        for template in _trace_templates(step):
            try:
                refs.extend(templates.references(template))
            except templates.TemplateError:
                pass  # recusado ao montar o passo
        if any(ref.scope is templates.Scope.VARS for ref in refs):
            # Onde o passo age depende da página: nenhum item que chega a ele conta.
            if not reads_page:
                for each, each_reach in loops:
                    each.forget(each_reach)
            return
        *outer, (loop, _) = loops
        if not reads_page:
            for each, each_reach in outer:
                # Para as repetições de fora, o passo é o mesmo em todos os itens delas.
                for index in each_reach:
                    if (trace := each.traces[index]) is not None:
                        trace.append(("passo", step.id))
        for index, item in rows[1]:
            scopes = {templates.Scope.INPUTS: values, templates.Scope.ITEM: item}
            try:
                entry = _page_trace(step, scopes)
            except templates.TemplateError:
                loop.forget([index])  # o valor que falta já foi recusado em outro lugar
                continue
            if not reads_page and (trace := loop.traces[index]) is not None:
                trace.append(entry)
            if acts_on_row and entry[1]:  # (ação, texto da linha, ...); vazio já é recusado
                loop.actions.setdefault(entry[:2], []).append((index, step, undecided))
                # Cada item de uma repetição de fora repete a de dentro inteira: dois
                # itens de fora que caem na mesma página agiriam de novo nas mesmas
                # linhas.
                for each, each_reach in outer:
                    uses = each.actions.setdefault(entry[:2], [])
                    uses.extend((outer_index, step, undecided) for outer_index in each_reach)

    @staticmethod
    def _branches(
        step: IfStep, values: Mapping[str, Any], rows: _Rows | None, undecided: _Undecided
    ) -> list[tuple[list[Step], _Rows | None, _Undecided]]:
        """Os ramos de um ``if`` que podem rodar, cada um com os itens que chegam a ele.

        Se a condição dá para avaliar antes (para todos os itens, numa repetição
        sobre uma entrada de lista), só entram os ramos que vão rodar, cada um com
        os seus itens. Senão, entram os dois, marcados como ramos que só a
        execução decide.
        """
        if rows is None:
            holds = _static_holds(step.condition, values, None)
            if holds is not None:
                return [(step.then if holds else step.otherwise, None, undecided)]
        else:
            name, items = rows
            decided = [
                (index, row, _static_holds(step.condition, values, row)) for index, row in items
            ]
            if all(holds is not None for _, _, holds in decided):
                then = [(index, row) for index, row, holds in decided if holds]
                otherwise = [(index, row) for index, row, holds in decided if not holds]
                return [
                    (block, (name, part), undecided)
                    for block, part in ((step.then, then), (step.otherwise, otherwise))
                    if part
                ]
        return [
            (step.then, rows, (*undecided, f"no ramo 'então' do passo '{step.id}'")),
            (step.otherwise, rows, (*undecided, f"no ramo 'senão' do passo '{step.id}'")),
        ]

    @staticmethod
    def _ref_values(
        ref: templates.Ref, values: Mapping[str, Any], rows: _Rows | None
    ) -> list[tuple[str, object]]:
        """Os valores que uma referência pode ter, com a descrição de cada um."""
        if ref.scope is templates.Scope.INPUTS and ref.name in values:
            return [(f"a entrada '{ref.name}'", values[ref.name])]
        if ref.scope is templates.Scope.ITEM and rows is not None:
            name, items = rows
            return [
                (f"o campo '{name}[{index}].{ref.name}'", row[ref.name])
                for index, row in items
                if ref.name in row
            ]
        return []  # variáveis (e referências sem valor, recusadas ao montar o passo)

    def _url_value_problems(
        self,
        step: NavigateStep,
        values: Mapping[str, Any],
        rows: _Rows | None,
        undecided: _Undecided,
    ) -> list[str]:
        try:
            parts = templates.parse(step.url)
        except templates.TemplateError:
            return []  # recusado ao montar o passo
        found: list[str] = []
        for index, slot in _url_slots(parts).items():
            ref = parts[index]
            place = _PATH_SLOTS.get(slot)
            if place is None or not isinstance(ref, templates.Ref):
                continue
            for where, value in self._ref_values(ref, values, rows):
                try:
                    text = templates.format_value(value)
                except templates.TemplateError:
                    continue  # já recusado como valor que não pode ser digitado
                problem = _path_value_problem(text)
                if problem is not None:
                    found.append(
                        f"{where} não pode ir {place} do passo '{step.id}': {problem}"
                        f"{_undecided_reason(undecided)}"
                    )
        return found

    @staticmethod
    def _row_key_problems(
        step: Step | None,
        target: Target,
        values: Mapping[str, Any],
        rows: _Rows | None,
        undecided: _Undecided,
    ) -> list[str]:
        """Valores de entradas e de itens que ficariam vazios no texto que escolhe o contêiner.

        Cada valor é conferido sozinho, como :func:`_row_key_value` faz quando o
        passo roda: ``Nota {{ item.numero }}`` com o número vazio não fica vazio,
        mas escolheria a linha por outro texto. Variáveis (e itens de uma tabela
        lida da página) só existem durante a execução e são conferidas lá.
        """
        template = target.within_has_text
        if template is None:
            return []
        try:
            refs = list(dict.fromkeys(templates.references(template)))
        except templates.TemplateError:
            return []  # recusado ao montar o passo
        where = f"no passo '{step.id}'" if step is not None else "nas pós-condições"
        found: list[str] = []
        for ref in refs:
            checks: list[tuple[str, Mapping[str, object]]]
            if ref.scope is templates.Scope.INPUTS:
                checks = [("", values)]
            elif ref.scope is templates.Scope.ITEM and rows is not None:
                name, items = rows
                checks = [(f" para '{name}[{index}]'", row) for index, row in items]
            else:
                continue  # variáveis e itens de uma tabela lida da página
            for suffix, scope_values in checks:
                if ref.name not in scope_values:
                    continue  # recusado ao montar o passo
                try:
                    text = templates.format_value(scope_values[ref.name])
                except templates.TemplateError:
                    continue  # já recusado como valor que não pode ser digitado
                if not _normalize_text(text):
                    found.append(
                        f"{ref} ficaria vazio no texto que escolhe o contêiner de "
                        f"'{target.intent}' {where}{suffix}{_undecided_reason(undecided)}"
                    )
        return found

    def _check_list_limits(self, values: Mapping[str, Any]) -> None:
        """Recusa, antes do primeiro passo, uma lista maior que a repetição que a percorre."""
        for step, _ in self._manifest.iter_steps():
            if not isinstance(step, ForEachStep):
                continue
            scope, name = step.items.split(".", 1)
            items = values.get(name) if scope == "inputs" else None
            if isinstance(items, list) and len(items) > step.max_items:
                err = self._too_many_items(step, len(items))
                err.step_id = step.id
                err.reported = True  # o passo nem começou: não há evento nem tela
                raise err

    @staticmethod
    def _too_many_items(step: ForEachStep, count: int) -> StepError:
        return StepError(
            ErrorCode.LIMITE_DE_ITENS_EXCEDIDO,
            f"'{step.items}' tem {count} itens, mais que o limite de "
            f"{step.max_items} do passo '{step.title}'.",
            details={"itens": count, "limite": step.max_items},
        )

    def _collect_outputs(self) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        for spec in self._manifest.outputs:
            name = spec.source.split(".", 1)[1]
            outputs[spec.name] = (
                self._output_value(spec.type, self._vars[name]) if name in self._vars else None
            )
        return outputs

    def _output_value(self, kind: FieldType, raw: Any) -> Any:
        """Valor de uma saída, já mascarado.

        A máscara vem antes da conversão de tipo: um segredo só de dígitos viraria
        número e escaparia dela. Se a máscara mudou algo, fica o texto mascarado.
        """
        if isinstance(raw, ArtifactRef):
            # Só o hash, calculado pelo próprio executor, fica fora da máscara.
            data = raw.model_dump(mode="json")
            return {
                key: item if key == "sha256" else self._redactor.value(item)
                for key, item in data.items()
            }
        masked = self._redactor.value(raw)
        if masked != raw:
            return masked
        converted = _coerce_output(kind, raw)
        if (
            isinstance(raw, str)
            and isinstance(converted, (int, float))
            and not isinstance(converted, bool)
            and any(
                self._redactor.text(text) != text for text in _number_texts(kind, raw, converted)
            )
        ):
            # A conversão juntou um segredo (por exemplo, tirando os pontos de um número).
            return MASK
        return converted

    async def _check_postconditions(self) -> None:
        conditions = self._manifest.postconditions
        if not conditions:
            return
        loop = asyncio.get_running_loop()
        if loop.time() >= self._deadline:
            raise StepError(
                ErrorCode.TEMPO_ESGOTADO,
                "O prazo da execução acabou antes de conferir as pós-condições.",
            )
        deadline = min(loop.time() + self._postcondition_timeout_s, self._deadline)
        for index, condition in enumerate(conditions, start=1):
            if not await self._wait_condition(condition, deadline):
                if deadline >= self._deadline:
                    raise StepError(
                        ErrorCode.TEMPO_ESGOTADO,
                        "O prazo da execução acabou enquanto as pós-condições eram conferidas.",
                    )
                raise StepError(
                    ErrorCode.POS_CONDICAO_FALHOU,
                    f"A pós-condição nº {index} ({_describe_condition(condition)}) "
                    "não foi satisfeita no fim da execução.",
                    details={"pos_condicao": index},
                )

    # ------------------------------------------------------------------
    # Eventos, telas e segurança
    # ------------------------------------------------------------------

    async def _emit(
        self, kind: RunEventKind, step_id: str | None, data: Mapping[str, Any] | None = None
    ) -> None:
        event = RunEvent(
            run_id=self._run_id,
            attempt=self._attempt,
            seq=self._seq,
            at=datetime.now(UTC),
            kind=kind,
            step_id=step_id,
            data=self._mask_event_data(data or {}),
        )
        self._seq += 1
        await self._events.emit(event)

    def _mask_event_data(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Mascara os dados de um evento.

        As chaves de primeiro nível são constantes do executor e nunca são
        mascaradas: o plano de controle decide por elas, e um segredo que fosse um
        pedaço de uma delas (``vers`` em ``irreversivel``) apagaria o aviso do passo
        irreversível, e o executor agiria sem esperar a confirmação. Também ficam
        intactos os valores que são constantes do executor (efeito, estado, código
        de erro, id de passo...) e os hashes que ele mesmo calcula, onde um segredo
        curto poderia aparecer por acaso. O resto, inclusive as chaves de dentro,
        que podem vir da página, é mascarado.
        """
        return {
            key: item if self._is_event_constant(key, item) else self._redactor.value(item)
            for key, item in data.items()
        }

    def _is_event_constant(self, key: str, item: Any) -> bool:
        if key in _HASH_KEYS:
            return isinstance(item, str) and _HEX_DIGEST.fullmatch(item) is not None
        allowed = self._event_constants.get(key)
        if allowed is None:
            return False
        if isinstance(item, str):
            return item in allowed
        return isinstance(item, list) and all(
            isinstance(value, str) and value in allowed for value in item
        )

    async def _emit_step_started(
        self, step: Step, effect: Effect | None = None, *, irreversible: bool = False
    ) -> None:
        """Anuncia um passo simples, com o efeito com que ele é tratado (:meth:`_effect`).

        O de um passo que grava leva ``"grava": true`` e o aviso de um passo
        irreversível, ``"irreversivel": true``: com confirmação
        (:class:`~rapa.runner.ports.AckingEventSink`), o executor só segue quando o
        plano de controle confirma que gravou o anúncio, e uma falha dela
        (:class:`~rapa.runner.errors.StepError`) vem depois de o anúncio ser registrado.
        """
        if effect is None:
            effect = self._effect(step)
        data: dict[str, Any] = {
            "tipo": step.kind,
            "titulo": step.title,
            "efeito": effect.value,
        }
        if self._items:
            data["iteracao"] = self._items[-1][1]
        if effect is Effect.WRITE:  # o executor espera a confirmação (ports.waits_for_ack)
            data["grava"] = True
        if irreversible:
            data["irreversivel"] = True
        await self._emit(RunEventKind.STEP_STARTED, step.id, data)

    async def _emit_block_started(self, step: ForEachStep | IfStep, effect: Effect) -> None:
        """Anuncia um contêiner, que nunca espera confirmação nem marca uma gravação.

        Quem grava são os passos de dentro, e cada um se anuncia antes de agir, com o
        efeito do contêiner se for maior que o dele. O efeito do contêiner vai em
        ``efeito_do_bloco``, nunca em ``efeito``: o plano de controle conta um passo
        anunciado com ``efeito`` ``write`` como uma gravação começada, e o contêiner
        ainda não gravou nada (se o executor parar aqui, a execução pode voltar para a
        fila).
        """
        data: dict[str, Any] = {
            "tipo": step.kind,
            "titulo": step.title,
            "efeito_do_bloco": effect.value,
        }
        if self._items:
            data["iteracao"] = self._items[-1][1]
        await self._emit(RunEventKind.STEP_STARTED, step.id, data)

    def _effect(self, step: Step) -> Effect:
        """O efeito com que um passo é anunciado e tratado ao agir (:func:`_effective_effect`).

        Num passo que age, é o dele ou, se for maior, o de um contêiner em volta; um
        passo que só lê fica com o seu.
        """
        return _effective_effect(step, self._block_effects)

    async def _screenshot(self, step_id: str | None, reason: Literal["depois", "falha"]) -> None:
        """Guarda a tela atual (melhor esforço: uma falha aqui não derruba a execução)."""
        page = self._page
        if page is None or page.is_closed():
            return
        self._screenshots += 1
        name = f"tela-{self._screenshots:03d}-{step_id or 'execucao'}-{reason}.png"
        try:
            data = await page.screenshot(
                type="png",
                full_page=reason == "falha",
                timeout=_SCREENSHOT_TIMEOUT_MS,
                animations="disabled",
                caret="hide",
                mask=list(self._secret_locators),
            )
            ref = await self._artifact_sink.save_bytes(ArtifactKind.SCREENSHOT, name, data)
        except Exception as exc:
            await self._emit(
                RunEventKind.ARTIFACT,
                step_id,
                {"tipo": ArtifactKind.SCREENSHOT.value, "erro": type(exc).__name__},
            )
            return
        await self._register_artifact(step_id, ref)

    async def _register_artifact(self, step_id: str | None, ref: ArtifactRef) -> None:
        self._artifacts.append(ref)
        await self._emit(
            RunEventKind.ARTIFACT,
            step_id,
            {"tipo": ref.kind, "nome": ref.name, "tamanho": ref.size, "sha256": ref.sha256},
        )

    async def _guard_request(self, route: Route) -> None:
        """Bloqueia requisições a domínios fora da lista do robô (como o proxy de saída)."""
        request = route.request
        url = request.url
        try:
            scheme = urlsplit(url).scheme
        except ValueError:
            scheme = ""
        host = _host(url)
        try:
            if scheme not in _DEFAULT_PORTS or host in self._allowed_hosts:
                await route.fallback()
                return
            if request.is_navigation_request():
                try:
                    main_frame = request.frame.parent_frame is None
                except PlaywrightError:
                    main_frame = False
                if main_frame:
                    self._blocked_navigation = host
            await route.abort("blockedbyclient")
        except PlaywrightError:
            # O contexto pode ter sido fechado no meio do roteamento.
            return

    def _check_page_domain(self, step: Step) -> None:
        blocked = self._blocked_navigation
        if blocked is not None:
            self._blocked_navigation = None
            raise StepError(
                ErrorCode.DOMINIO_NAO_PERMITIDO,
                f"O passo '{step.title}' tentou abrir '{blocked}', que não está nos domínios "
                "permitidos do robô.",
                details={"dominio": blocked},
            )
        page = self._page
        if page is None:
            return
        url = page.url
        host = _host(url)
        if urlsplit(url).scheme in _DEFAULT_PORTS and host not in self._allowed_hosts:
            raise StepError(
                ErrorCode.DOMINIO_NAO_PERMITIDO,
                f"Depois do passo '{step.title}', a página está em '{host}', fora dos domínios "
                "permitidos do robô.",
                details={"dominio": host},
            )

    def _raise_if_run_deadline(self, ctx: _StepCtx) -> None:
        """Se a espera acabou pelo prazo total da execução, o erro é ``tempo_esgotado``."""
        if ctx.deadline >= self._deadline:
            raise StepError(
                ErrorCode.TEMPO_ESGOTADO,
                f"O prazo da execução acabou durante o passo '{ctx.step.title}'.",
            )

    def _detail(self, exc: BaseException) -> str:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return self._redactor.text(first)[:_DETAIL_MAX]

    def _convert(self, step: Step, exc: Exception) -> StepError:
        """Traduz uma exceção do navegador (ou inesperada) num erro do passo."""
        if self._blocked_navigation is not None:
            try:
                self._check_page_domain(step)
            except StepError as err:
                return err
        loop = asyncio.get_running_loop()
        if isinstance(exc, (PlaywrightTimeoutError, TimeoutError)):
            if loop.time() >= self._deadline - 0.05:
                return StepError(
                    ErrorCode.TEMPO_ESGOTADO,
                    f"O prazo da execução acabou durante o passo '{step.title}'.",
                )
            return StepError(
                ErrorCode.TEMPO_DO_PASSO_ESGOTADO,
                f"O passo '{step.title}' passou de {step.timeout_ms} ms sem terminar.",
                details={"detalhe": self._detail(exc)},
            )
        if isinstance(exc, PlaywrightError):
            return StepError(
                ErrorCode.ACAO_FALHOU,
                f"O navegador não conseguiu concluir o passo '{step.title}'.",
                details={"detalhe": self._detail(exc)},
            )
        return StepError(
            ErrorCode.ERRO_INTERNO,
            f"Erro interno do executor no passo '{step.title}' ({type(exc).__name__}).",
        )

    # ------------------------------------------------------------------
    # Passos
    # ------------------------------------------------------------------

    async def _run_block(self, steps: list[Step]) -> None:
        for step in steps:
            await self._run_step(step)

    async def _run_step(self, step: Step) -> None:
        loop = asyncio.get_running_loop()
        if self._cancel is not None and self._cancel.is_set():
            raise _Cancelled(step)
        if loop.time() >= self._deadline:
            err = StepError(
                ErrorCode.TEMPO_ESGOTADO,
                f"O prazo da execução acabou antes do passo '{step.title}'.",
            )
            err.step_id = step.id
            err.reported = True  # o passo nem começou: não há evento nem tela
            raise err
        if self._mode is RunMode.UNTIL_FIRST_WRITE and self._effect(step) is not Effect.READ:
            raise _StopBeforeWrite(step)
        if isinstance(step, (ForEachStep, IfStep)):
            await self._run_container(step)
        else:
            await self._run_leaf(step)

    async def _run_leaf(self, step: Step) -> None:
        """Roda um passo simples: anuncia, prepara, confere a rede contra repetições e age.

        Um passo que grava (``write``, também pelo efeito de um contêiner em volta,
        se ele age na página: :meth:`_effect`) se anuncia antes de preparar e, com
        confirmação, só segue quando o plano de controle a dá; o prazo do passo
        começa depois dela. O passo irreversível só avisa depois de encontrar o alvo;
        a espera pela confirmação do aviso tem só o prazo da própria confirmação, e
        o prazo do passo fica parado até ela chegar. Se a confirmação não vem, o
        passo falha como qualquer outro (evento ``step_failed`` dele), sem ter agido.
        Antes de agir, um passo que grava confere de novo o alvo (depois da espera
        pelo aviso de passo irreversível, também a linha e a rede) e prende o
        elemento conferido, onde a ação vai (:meth:`_confirm_target`).

        Uma ação que grava numa linha escolhida pelo texto é anotada antes de agir
        (:meth:`_record_act`); daí até o fim de cada volta em volta dela, cada
        endereço por onde a página principal passa (pedido de navegação,
        redirecionamento ou ``pushState``) também é página dela, inclusive pelos
        passos seguintes da volta (a confirmação de um modal, um link). Uma volta que
        acabou continua contando as navegações da página principal até o fim da
        repetição, inclusive as que um clique de uma volta seguinte provoca (na
        dúvida, a navegação pode ser um temporizador ou um popup da ação anterior),
        menos as do próprio passo ``navigate`` do robô (:meth:`_on_request`): um
        clique sem ``expect_navigation`` volta antes de a resposta do formulário
        chegar, e um ``setTimeout`` da página ou um popup pode levá-la a outro
        endereço muito depois.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        effect = self._effect(step)
        irreversible = effect is Effect.IRREVERSIBLE
        announced = False
        try:
            if not irreversible:
                try:
                    await self._emit_step_started(step, effect)
                finally:
                    announced = True  # uma falha da confirmação vem depois do registro
            # O prazo do passo começa agora: a espera pela confirmação não o consome.
            ctx = _StepCtx(step, min(loop.time() + step.timeout_ms / 1000, self._deadline))
            try:
                async with asyncio.timeout_at(ctx.deadline + _TIMEOUT_GRACE_S) as timeout:
                    action = await self._prepare(step, ctx)
                    # Com o alvo já encontrado, a página é a dele; e ainda antes do aviso
                    # de passo irreversível.
                    act = self._row_not_acted_on(step)
                    page = _page_key(self.page.url)
                    if irreversible:
                        # O aviso vai antes: com confirmação, ele pode esperar o plano de
                        # controle gravá-lo. Se falhar, nada foi enviado ao portal. A
                        # espera tem o prazo da própria confirmação, e o do passo fica
                        # parado até ela chegar.
                        timeout.reschedule(None)
                        waiting_since = loop.time()
                        try:
                            await self._emit_step_started(step, effect, irreversible=True)
                        finally:
                            announced = True
                        if loop.time() >= self._deadline:
                            # Nada foi enviado ao portal: o passo não age depois do prazo.
                            raise StepError(
                                ErrorCode.TEMPO_ESGOTADO,
                                f"O prazo da execução acabou durante o passo '{step.title}'.",
                            )
                        ctx.deadline = min(
                            ctx.deadline + loop.time() - waiting_since, self._deadline
                        )
                        timeout.reschedule(ctx.deadline + _TIMEOUT_GRACE_S)
                    try:
                        if effect is not Effect.READ and isinstance(step, _ACTS_ON_TARGET):
                            # Nada foi enviado ao portal se a conferência falhar aqui.
                            act = await self._confirm_target(
                                step, ctx, page, act, again=irreversible
                            )
                        if irreversible:
                            # Daqui em diante, qualquer falha deixa a execução indeterminada.
                            self._irreversible_step = step
                        if act is not None:
                            # Anotada antes de agir: uma ação que falhou no meio também conta.
                            act = self._record_act(act)
                        if not _refreshes_typed(step):
                            held: set[int] = set()
                        elif ctx.held is not None:
                            held = ctx.held  # a conferência antes de gravar já leu
                        else:
                            held = await self._typed_held()
                        try:
                            data = await action()
                        finally:
                            if act is not None:
                                # Onde ela parou (só nas voltas dela: não é uma navegação).
                                self._visit(self.page.url)
                    finally:
                        # O vigia que sobrou de um passo que falhou antes de agir sai da página.
                        await self._guard_refused(ctx)
                        if ctx.element is not None:
                            await self._dispose(ctx.element)
                self._check_page_domain(step)
            except StepError:
                raise
            except Exception as exc:
                raise self._convert(step, exc) from None
        except StepError as err:
            if not announced:
                await self._emit_step_started(step, effect)
            await self._report_failure(step, err, started)
            raise
        if _refreshes_typed(step):
            # O que o passo mudou nos campos digitados vira a referência deles. O que muda
            # enquanto um passo só lê (uma espera) ou digita noutro campo é da própria
            # página (a resposta do CEP que chega enquanto o robô digita o número).
            await self._refresh_typed(held)
        self._steps_done += 1
        await self._emit(
            RunEventKind.STEP_SUCCEEDED,
            step.id,
            {"duracao_ms": round((loop.time() - started) * 1000), **data},
        )
        if self._screenshot_every_step or effect is not Effect.READ:
            await self._screenshot(step.id, "depois")

    async def _report_failure(self, step: Step, err: StepError, started: float) -> None:
        loop = asyncio.get_running_loop()
        duration = round((loop.time() - started) * 1000)
        if err.reported:
            # Falhou num passo de dentro: o contêiner também registra a falha.
            await self._emit(
                RunEventKind.STEP_FAILED,
                step.id,
                {
                    "codigo": str(err.code),
                    "mensagem": err.message,
                    "duracao_ms": duration,
                    "passo_interno": err.step_id,
                },
            )
            return
        err.step_id = step.id
        err.reported = True
        await self._emit(
            RunEventKind.STEP_FAILED,
            step.id,
            {
                "codigo": str(err.code),
                "mensagem": err.message,
                "duracao_ms": duration,
                **err.details,
            },
        )
        await self._screenshot(step.id, "falha")

    async def _run_container(self, step: ForEachStep | IfStep) -> None:
        """Roda uma repetição ou um ``if``.

        O contêiner não age: ele se anuncia sem esperar confirmação e sem marcar uma
        gravação (:meth:`_emit_block_started`), e o efeito dele vale para cada passo
        de dentro que age na página (:meth:`_effect`), que se anuncia antes de agir.
        Se nada de dentro começou, nada foi gravado. Ao fim de cada volta, as
        navegações da página principal continuam contando para as ações dela até o
        fim da repetição, menos as do passo ``navigate`` do robô (:meth:`_run_leaf`).
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        # O prazo do contêiner não limita os passos de dentro: cada um tem o seu.
        effect = self._effect(step)
        await self._emit_block_started(step, effect)
        self._block_effects.append(effect)
        try:
            if isinstance(step, ForEachStep):
                items = self._loop_items(step)
                for index, item in enumerate(items):
                    self._items.append((item, index, step.id))
                    try:
                        await self._run_block(step.steps)
                    finally:
                        self._items.pop()
                data: dict[str, Any] = {"itens": len(items)}
            else:
                holds = await self._holds(step.condition)
                await self._run_block(step.then if holds else step.otherwise)
                data = {"ramo": "entao" if holds else "senao"}
        except StepError as err:
            await self._report_failure(step, err, started)
            raise
        finally:
            self._block_effects.pop()
        self._steps_done += 1
        await self._emit(
            RunEventKind.STEP_SUCCEEDED,
            step.id,
            {"duracao_ms": round((loop.time() - started) * 1000), **data},
        )

    def _loop_items(self, step: ForEachStep) -> list[Mapping[str, object]]:
        scope, name = step.items.split(".", 1)
        source = self._inputs if scope == "inputs" else self._vars
        value = source.get(name)
        if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
            raise StepError(
                ErrorCode.LISTA_INVALIDA,
                f"'{step.items}' não é uma lista de itens no passo '{step.title}'.",
            )
        if len(value) > step.max_items:
            # Listas de entradas já foram conferidas antes do primeiro passo; aqui
            # sobram as tabelas extraídas da página.
            raise self._too_many_items(step, len(value))
        return list(value)

    async def _prepare(self, step: Step, ctx: _StepCtx) -> _Action:
        """Prepara o passo sem efeitos colaterais e devolve a ação que o executa."""
        match step:
            case NavigateStep():
                return self._prepare_navigate(step, ctx)
            case ClickStep():
                return await self._prepare_click(step, ctx)
            case FillStep():
                return await self._prepare_fill(step, ctx)
            case SelectStep():
                return await self._prepare_select(step, ctx)
            case CheckStep():
                return await self._prepare_check(step, ctx)
            case PressStep():
                return await self._prepare_press(step, ctx)
            case WaitForStep():
                return self._prepare_wait_for(step, ctx)
            case ExtractTextStep():
                return await self._prepare_extract_text(step, ctx)
            case ExtractTableStep():
                return await self._prepare_extract_table(step, ctx)
            case AssertStep():
                return self._prepare_assert(step, ctx)
            case DownloadStep():
                return await self._prepare_download(step, ctx)
            case UploadStep():
                return await self._prepare_upload(step, ctx)
            case ForEachStep() | IfStep():  # pragma: no cover - tratados como contêiner
                raise AssertionError("passo de controle tratado como passo simples")
            case _:  # pragma: no cover - conjunto fechado
                assert_never(step)

    @property
    def page(self) -> Page:
        if self._page is None:  # pragma: no cover - a página é aberta antes do 1º passo
            raise RuntimeError("a página ainda não foi aberta")
        return self._page

    def _prepare_navigate(self, step: NavigateStep, ctx: _StepCtx) -> _Action:
        url = self._render_url(step)
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            parts, host = urlsplit(""), ""
        if not host or host not in self._allowed_hosts:
            raise StepError(
                ErrorCode.DOMINIO_NAO_PERMITIDO,
                f"O passo '{step.title}' quer abrir '{host or 'um endereço sem domínio'}', "
                "que não está nos domínios permitidos do robô.",
                details={"dominio": host},
            )
        if parts.scheme != "https" and not (parts.scheme == "http" and host.endswith(TEST_TLD)):
            raise StepError(
                ErrorCode.DOMINIO_NAO_PERMITIDO,
                f"O passo '{step.title}' quer abrir um endereço sem https.",
                details={"dominio": host},
            )
        if parts.username or parts.password:
            raise StepError(
                ErrorCode.DOMINIO_NAO_PERMITIDO,
                f"O endereço do passo '{step.title}' não pode conter usuário ou senha.",
                details={"dominio": host},
            )

        async def act() -> dict[str, Any]:
            # A navegação que este passo faz não conta nas voltas que já acabaram.
            self._robot_navigation = _page_key(url)
            try:
                response = await self.page.goto(url, timeout=ctx.remaining_ms(), wait_until="load")
            except PlaywrightTimeoutError:
                raise
            except PlaywrightError as exc:
                if self._blocked_navigation is not None:
                    self._check_page_domain(step)
                raise StepError(
                    ErrorCode.NAVEGACAO_FALHOU,
                    f"Não consegui abrir a página do passo '{step.title}'.",
                    details={"detalhe": self._detail(exc)},
                ) from None
            finally:
                self._robot_navigation = None
                self._robot_requests.clear()
            return {"dominio": host, "status": response.status if response is not None else None}

        return act

    def _render_url(self, step: NavigateStep) -> str:
        """Monta o endereço codificando cada valor como um único trecho.

        ``?``, ``#`` ou ``&`` num valor nunca mudam o caminho nem a consulta; no
        caminho e na rota do fragmento (do ``#`` até um ``?`` dentro dele), valores
        vazios, com ``/``, ``\\`` ou ``%`` ou iguais a ``.`` e ``..`` (também na
        forma NFKC) são recusados. Domínio, porta e usuário vêm só do trecho
        literal do começo. Valores de entradas e itens já foram conferidos antes
        do primeiro passo; aqui a conferência pega os de variáveis.
        """
        try:
            slots = _url_slots(templates.parse(step.url))
        except templates.TemplateError:
            slots = {}  # _render recusa o template antes de codificar qualquer valor
        url = self._render(
            step.url,
            encode=lambda index, text: _encode_url_value(text, slots.get(index, "caminho")),
        )
        parts = templates.parse(step.url)  # já conferido por _render
        prefix = parts[0] if parts and isinstance(parts[0], str) else ""
        try:
            fixed = urlsplit(prefix).netloc.lower() == urlsplit(url).netloc.lower()
        except ValueError:
            fixed = False
        if not fixed:
            raise StepError(
                ErrorCode.DOMINIO_NAO_PERMITIDO,
                f"O domínio do endereço do passo '{step.title}' precisa ser fixo; só o "
                "caminho e a consulta podem usar valores.",
                details={"dominio": _host(url)},
            )
        return url

    async def _prepare_click(self, step: ClickStep, ctx: _StepCtx) -> _Action:
        locator = await self._resolve(step.target, step, ctx)
        navigation_owner: Page | Frame = self.page
        if step.expect_navigation and step.target.frames:
            frame = await self._owner_frame(locator, ctx)
            if frame is not None:
                navigation_owner = frame

        async def act() -> dict[str, Any]:
            if step.expect_navigation:
                async with navigation_owner.expect_navigation(
                    timeout=ctx.remaining_ms(), wait_until="load"
                ):
                    await self._act_on(step, ctx, locator, lambda on, kw: on.click(**kw))
            else:
                await self._act_on(step, ctx, locator, lambda on, kw: on.click(**kw))
            return {}

        return act

    async def _prepare_fill(self, step: FillStep, ctx: _StepCtx) -> _Action:
        value = step.value
        if not isinstance(value, SecretValue):
            text = self._render(value.template)
            locator = await self._resolve(step.target, step, ctx)
            writes = self._effect(step) is not Effect.READ

            async def act_text() -> dict[str, Any]:
                if ctx.element is None:
                    await self._type(step, ctx, locator, text)
                else:
                    await self._dispatched(step, ctx, self._type(step, ctx, locator, text))
                value = await self._field_value(ctx, locator)
                if value != text:
                    # A página mudou o campo (o CEP que preenche o logradouro quando o
                    # foco sai dele, e o texto digitado ficaria no fim do que ela pôs, ou
                    # uma máscara). Num passo que grava, nada que gravou é repetido: vale
                    # o texto como o navegador ou a máscara da página o guardam.
                    if writes:
                        if not await self._typed_matches(ctx, locator, text, value):
                            raise self._field_changed(step)
                    else:
                        first = value
                        await self._type(step, ctx, locator, text)
                        value = await self._field_value(ctx, locator)
                        if value not in (text, first):
                            raise self._field_changed(step)
                await self._remember_typed(step, ctx, locator, text, value)
                return {"origem": "texto", "tamanho": len(text)}

            return act_text

        credential = self._credentials.get(value.credential)
        if credential is None:
            raise StepError(
                ErrorCode.CREDENCIAL_DESCONHECIDA,
                f"A credencial '{value.credential}' do passo '{step.title}' não foi declarada.",
            )
        locator = await self._resolve(step.target, step, ctx)
        # O segredo vai para o elemento exato que foi conferido: se a página
        # navegar entre a conferência e a digitação, o elemento deixa de existir.
        handle = await self._element_handle(locator, ctx)
        frame = await handle.owner_frame()
        origin = _normalize_origin(frame.url) if frame is not None else ""
        expected = _normalize_origin(credential.origin)
        if not origin or origin != expected:
            await handle.dispose()
            raise StepError(
                ErrorCode.CREDENCIAL_ORIGEM_INVALIDA,
                f"O passo '{step.title}' digitaria a credencial '{credential.name}' em "
                f"'{origin or 'uma página sem origem'}', mas ela só pode ser usada em '{expected}'.",
                details={"origem": origin, "origem_da_credencial": expected},
            )
        try:
            secret = await self._secrets.get(credential, value.field)
        except SecretUnavailable as exc:
            await handle.dispose()
            raise StepError(
                ErrorCode.SEGREDO_INDISPONIVEL,
                f"O segredo do passo '{step.title}' não está disponível: {exc}",
            ) from None
        if value.field != "totp":
            # O código TOTP vale por segundos e nunca entra em eventos; mascarar
            # seis dígitos estragaria hashes e números legítimos.
            self._redactor.add(secret)
        self._secret_locators.append(locator)

        async def act_secret() -> dict[str, Any]:
            try:
                if step.sequential:
                    await handle.fill("", timeout=ctx.remaining_ms())
                    await handle.type(secret, timeout=ctx.remaining_ms())
                else:
                    await handle.fill(secret, timeout=ctx.remaining_ms())
            finally:
                await self._dispose(handle)
            return {"origem": "segredo", "tamanho": len(secret)}

        return act_secret

    async def _type(self, step: FillStep, ctx: _StepCtx, locator: PwLocator, text: str) -> None:
        """Digita ``text`` no campo, no lugar do que houver nele.

        No elemento conferido (num passo que grava), sem esperar de novo pelo campo: a
        espera já foi feita antes da conferência da hora de agir (:meth:`_confirm_target`).
        """
        element = ctx.element
        if element is None:
            if step.sequential:
                await locator.clear(timeout=ctx.remaining_ms())
                await locator.press_sequentially(text, timeout=ctx.remaining_ms())
            else:
                await locator.fill(text, timeout=ctx.remaining_ms())
        elif step.sequential:
            await element.fill("", force=True, timeout=ctx.remaining_ms())
            await element.type(text, timeout=ctx.remaining_ms())
        else:
            await element.fill(text, force=True, timeout=ctx.remaining_ms())

    async def _field_value(self, ctx: _StepCtx, locator: PwLocator) -> str:
        """O valor que o campo tem agora (o texto, num elemento ``contenteditable``)."""
        on = self._on(ctx, locator)
        if isinstance(on, ElementHandle):
            shown = await on.evaluate(_EDITABLE_TEXT_JS)
        else:
            shown = await on.evaluate(_EDITABLE_TEXT_JS, timeout=ctx.remaining_ms())
        if isinstance(shown, str):
            return shown
        return await on.input_value(timeout=ctx.remaining_ms())

    async def _typed_matches(
        self, ctx: _StepCtx, locator: PwLocator, text: str, value: str
    ) -> bool:
        """Se ``value``, o valor do campo, é ``text`` como o navegador ou a máscara da página
        o guardam (:func:`_typed_value_matches`). Nunca anota os valores."""
        on = self._on(ctx, locator)
        if isinstance(on, ElementHandle):
            state = await on.evaluate(_TYPED_STATE_JS, text)
        else:
            state = await on.evaluate(_TYPED_STATE_JS, text, timeout=ctx.remaining_ms())
        _, _, browser, number = state
        return _typed_value_matches(
            text,
            value,
            browser=browser if isinstance(browser, str) else None,
            number=number is True,
        )

    async def _remember_typed(
        self, step: FillStep, ctx: _StepCtx, locator: PwLocator, text: str, value: str
    ) -> None:
        """Anota o campo em que o texto foi digitado, para conferir antes de gravar.

        Um campo digitado de novo fica só com a última anotação. Se o campo não puder ser
        preso agora (a página o tirou logo depois), ele não é anotado.
        """
        try:
            if ctx.element is not None:
                copy = await ctx.element.evaluate_handle("(element) => element")
                handle = copy.as_element()
                if handle is None:
                    await self._dispose(copy)
                    return
            else:
                handle = await locator.element_handle(timeout=ctx.remaining_ms())
        except PlaywrightError:
            return
        try:
            frame = await handle.owner_frame()
        except PlaywrightError:
            frame = None
        entries = self._typed
        same = await self._same_elements(handle, [entry.handle for entry in entries])
        typed = _Typed(handle, text, value, step.target.intent, step.title, frame)
        if self._typed is not entries:
            # Outra conferência trocou a lista no meio: o campo entra na nova.
            self._typed.append(typed)
            return
        kept: list[_Typed] = []
        for entry, is_same in zip(entries, same, strict=True):
            if is_same:
                await self._dispose(entry.handle)
            else:
                kept.append(entry)
        if self._typed is entries:
            self._typed = [*kept, typed]
        else:
            self._typed.append(typed)

    async def _same_elements(
        self, handle: ElementHandle, others: list[ElementHandle]
    ) -> list[bool]:
        """Quais de ``others`` são o mesmo elemento que ``handle``."""
        if not others:
            return []
        try:
            same = await handle.evaluate(_SAME_ELEMENTS_JS, others)
            if isinstance(same, list) and len(same) == len(others):
                return [item is True for item in same]
        except PlaywrightError:
            pass  # algum é de outro documento: um por um
        result: list[bool] = []
        for other in others:
            try:
                result.append(await handle.evaluate(_SAME_ELEMENT_JS, other) is True)
            except PlaywrightError:
                result.append(False)
        return result

    async def _refresh_typed(self, held: set[int]) -> None:
        """No fim de cada passo do robô que age na página sem digitar (um clique, uma
        tecla, uma escolha; não os que só leem, como uma espera, :data:`_PAGE_READS`, nem
        os que digitam, que anotam só o próprio campo), o valor de agora de cada campo
        digitado vira a referência dele (``accepted``).

        O que muda durante esses passos é aceito: a sugestão escolhida numa busca com
        autocompletar, a busca que a página limpa depois do Enter. O que a página muda
        sozinha num campo enquanto o robô espera ou digita noutro campo (o CEP que
        preenche o logradouro enquanto o robô digita o número ou espera o aviso) é
        recusado antes de um passo que grava (:meth:`_typed_fields_hold`); a formatação
        que a página põe quando o foco sai do campo vale pela comparação
        (:func:`_typed_value_matches`). Só ganham a referência nova os campos que ainda a
        tinham logo antes de o passo agir (``held``, de :meth:`_typed_held`): o campo
        que a página já tinha mudado antes (durante uma espera) fica com a antiga, e o
        próximo passo que grava falha. O campo que saiu da página (ou cujo documento
        acabou) sai da lista. Nunca falha o passo e nunca anota os valores.
        """
        entries = self._typed
        if not entries:
            return
        states = await self._typed_states_soon(entries)
        if states is None:
            return
        kept: list[_Typed] = []
        gone: list[ElementHandle] = []
        for entry, state in zip(entries, states, strict=True):
            if state is None:
                gone.append(entry.handle)
                continue
            value = state[0] if isinstance(state, list) and state else None
            if isinstance(value, str) and id(entry.handle) in held:
                entry = entry._replace(accepted=value)
            kept.append(entry)
        if self._typed is entries:  # senão, outro passo mexeu na lista no meio
            self._typed = kept
            for handle in gone:
                await self._dispose(handle)

    async def _typed_fields_hold(
        self, step: Step, current: ElementHandle | None
    ) -> list[tuple[_Typed, str]]:
        """Antes de um passo que grava: os campos de texto que o robô digitou ainda têm o
        valor da referência deles (a digitação, ou o fim do último clique, tecla ou
        escolha do robô).

        A página pode mudar um campo depois da conferência da digitação, sozinha (o CEP
        que preenche o logradouro depois de uma consulta), e o passo que grava enviaria um
        valor que ninguém digitou. Cada campo vale com o valor da referência
        (:meth:`_refresh_typed`: o que mudou durante um clique, uma tecla ou uma escolha do
        robô, como a sugestão de um autocompletar, já é a referência), ou com o texto como o
        navegador ou a máscara da página o guardam (:func:`_typed_value_matches`). Um campo
        que saiu da página, ou cujo documento acabou, deixa de ser conferido, e o campo em
        que o passo vai digitar agora (``current``) também. Se algum mudou, o passo falha
        com ``acao_falhou`` sem agir; os valores nunca vão para a mensagem. Devolve o
        valor que leu de cada campo (para o vigia conferir de novo no primeiro evento da
        ação: :meth:`_arm_guard`; os campos lidos são os que ainda têm a referência, para
        :meth:`_refresh_typed`).
        """
        entries = self._typed
        if not entries:
            return []
        states = await self._typed_states(entries, current)
        kept: list[_Typed] = []
        gone: list[ElementHandle] = []
        read: list[tuple[_Typed, str]] = []
        changed: _Typed | None = None
        for entry, state in zip(entries, states, strict=True):
            if state is None:
                gone.append(entry.handle)
                continue
            kept.append(entry)
            if state == "mesmo" or changed is not None:
                continue
            value, _, browser, number = state
            holds = isinstance(value, str) and (
                value == entry.accepted
                or _typed_value_matches(
                    entry.text,
                    value,
                    browser=browser if isinstance(browser, str) else None,
                    number=number is True,
                )
            )
            if not holds:
                changed = entry
            elif isinstance(value, str):
                read.append((entry, value))
        if self._typed is entries:  # senão, a página principal navegou no meio
            self._typed = kept
            for handle in gone:
                await self._dispose(handle)
        if changed is not None:
            raise self._typed_changed(step, changed.intent, changed.title)
        return read

    async def _typed_held(self) -> set[int]:
        """Os campos digitados que ainda têm o valor da referência (ou o texto, como o
        navegador ou a máscara da página o guardam): só esses ganham uma referência nova no
        fim do passo (:meth:`_refresh_typed`). Nunca falha e nunca anota os valores."""
        entries = self._typed
        if not entries:
            return set()
        states = await self._typed_states_soon(entries)
        held: set[int] = set()
        if states is None:
            return held
        for entry, state in zip(entries, states, strict=True):
            if not isinstance(state, list) or len(state) < 4:
                continue
            value, _, browser, number = state
            if isinstance(value, str) and (
                value == entry.accepted
                or _typed_value_matches(
                    entry.text,
                    value,
                    browser=browser if isinstance(browser, str) else None,
                    number=number is True,
                )
            ):
                held.add(id(entry.handle))
        return held

    @staticmethod
    def _typed_changed(step: Step, intent: str, title: str) -> StepError:
        # Nunca com os valores: o campo pode ser sigiloso.
        return StepError(
            ErrorCode.ACAO_FALHOU,
            f"O campo '{intent}' (passo '{title}') não tem mais o que o robô digitou: a "
            f"página o mudou depois. O passo '{step.title}' enviaria um valor que não foi "
            "digitado, então nada foi feito.",
            details={"alvo": intent},
        )

    async def _typed_states_soon(self, entries: list[_Typed]) -> list[Any] | None:
        """:meth:`_typed_states` com um prazo curto (uma página travada não prende a
        execução); ``None`` se não deu."""
        try:
            async with asyncio.timeout(_TYPED_READ_S):
                return await self._typed_states(entries, None)
        except TimeoutError, PlaywrightError:
            return None

    async def _typed_states(
        self, entries: list[_Typed], current: ElementHandle | None
    ) -> list[Any]:
        """O estado de cada campo digitado (:data:`_TYPED_FIELD_JS`); ``None`` quando o
        documento dele não existe mais."""
        handles = [entry.handle for entry in entries]
        texts = [entry.text for entry in entries]
        try:
            states = await handles[0].evaluate(_TYPED_FIELDS_JS, [handles, texts, current])
            if isinstance(states, list) and len(states) == len(handles):
                return states
        except PlaywrightError:
            pass  # campos de outros documentos: um por um
        result: list[Any] = []
        for handle, text in zip(handles, texts, strict=True):
            state = None
            for other in (current, None):
                try:
                    state = await handle.evaluate(_TYPED_FIELD_JS, [text, other])
                    break
                except PlaywrightError:
                    continue
            result.append(state)
        return result

    @staticmethod
    def _field_changed(step: FillStep) -> StepError:
        # Nunca com os valores: o campo pode ser sigiloso.
        return StepError(
            ErrorCode.ACAO_FALHOU,
            f"Depois de digitar no campo '{step.target.intent}' (passo '{step.title}'), o "
            "valor dele não é o que o robô digitou: a página mudou o campo. O passo parou "
            "aqui, e nada mais foi feito.",
            details={"alvo": step.target.intent},
        )

    async def _prepare_select(self, step: SelectStep, ctx: _StepCtx) -> _Action:
        option = self._render(step.option)
        locator = await self._resolve(step.target, step, ctx)

        async def act() -> dict[str, Any]:
            await self._act_on(
                step, ctx, locator, lambda on, kw: on.select_option(label=option, **kw)
            )
            return {}

        return act

    async def _prepare_check(self, step: CheckStep, ctx: _StepCtx) -> _Action:
        locator = await self._resolve(step.target, step, ctx)

        async def act() -> dict[str, Any]:
            await self._act_on(
                step, ctx, locator, lambda on, kw: on.set_checked(step.checked, **kw)
            )
            return {"marcado": step.checked}

        return act

    async def _prepare_press(self, step: PressStep, ctx: _StepCtx) -> _Action:
        if step.target is None:

            async def act_page() -> dict[str, Any]:
                await self.page.keyboard.press(step.key)
                return {"tecla": step.key}

            return act_page
        locator = await self._resolve(step.target, step, ctx)

        async def act() -> dict[str, Any]:
            await self._act_on(step, ctx, locator, lambda on, kw: on.press(step.key, **kw))
            return {"tecla": step.key}

        return act

    def _prepare_wait_for(self, step: WaitForStep, ctx: _StepCtx) -> _Action:
        needle = self._render(step.url_contains) if step.url_contains is not None else None

        async def act() -> dict[str, Any]:
            if needle is not None:
                await self.page.wait_for_url(
                    lambda url: needle in url, timeout=ctx.remaining_ms(), wait_until="load"
                )
            if step.target is not None:
                if step.state == "visible":
                    locator = await self._resolve(step.target, step, ctx)
                    await locator.wait_for(state="visible", timeout=ctx.remaining_ms())
                else:
                    await self._wait_hidden(step.target, ctx)
            return {}

        return act

    async def _prepare_extract_text(self, step: ExtractTextStep, ctx: _StepCtx) -> _Action:
        locator = await self._resolve(step.target, step, ctx)

        async def act() -> dict[str, Any]:
            text = (await locator.inner_text(timeout=ctx.remaining_ms())).strip()
            self._vars[step.into] = text
            return {"tamanho": len(text)}

        return act

    async def _prepare_extract_table(self, step: ExtractTableStep, ctx: _StepCtx) -> _Action:
        locator = await self._resolve(step.target, step, ctx)

        async def act() -> dict[str, Any]:
            raw = await locator.evaluate(_READ_TABLE_JS, step.max_rows, timeout=ctx.remaining_ms())
            # Cabeçalhos viram chaves: mascarados antes, os repetidos viram coluna_N.
            headers = [self._redactor.text(str(h)) for h in raw.get("headers", [])]
            rows = [[str(cell) for cell in row] for row in raw.get("rows", [])]
            keys = self._table_keys(step.columns, headers, rows)
            table = [
                {key: (cells[i] if i < len(cells) else "") for i, key in enumerate(keys)}
                for cells in rows
            ]
            self._vars[step.into] = table
            total = int(raw.get("total", len(table)))
            return {"linhas": len(table), "colunas": len(keys), "truncada": total > len(table)}

        return act

    @staticmethod
    def _table_keys(
        columns: list[str] | None, headers: list[str], rows: list[list[str]]
    ) -> list[str]:
        if columns:
            return list(columns)
        width = max([len(headers), *(len(row) for row in rows)], default=0)
        keys: list[str] = []
        for index in range(width):
            key = headers[index] if index < len(headers) and headers[index] else ""
            if not key or key in keys:
                key = f"coluna_{index + 1}"
            keys.append(key)
        return keys

    def _prepare_assert(self, step: AssertStep, ctx: _StepCtx) -> _Action:
        async def act() -> dict[str, Any]:
            if not await self._wait_condition(step.condition, ctx.deadline):
                self._raise_if_run_deadline(ctx)
                raise StepError(
                    ErrorCode.VERIFICACAO_FALHOU,
                    step.message,
                    details={"condicao": _describe_condition(step.condition)},
                )
            return {}

        return act

    async def _prepare_download(self, step: DownloadStep, ctx: _StepCtx) -> _Action:
        name = self._artifact_name(self._render(step.save_as), default=f"{step.id}.bin")
        locator = await self._resolve(step.target, step, ctx)

        async def act() -> dict[str, Any]:
            async with self.page.expect_download(timeout=ctx.remaining_ms()) as info:
                await self._act_on(step, ctx, locator, lambda on, kw: on.click(**kw))
            download = await info.value
            try:
                failure = await download.failure()
                if failure is not None:
                    raise StepError(
                        ErrorCode.DOWNLOAD_FALHOU,
                        f"O download do passo '{step.title}' falhou.",
                        details={"detalhe": self._redactor.text(failure)[:_DETAIL_MAX]},
                    )
                source = await download.path()
                try:
                    ref = await self._artifact_sink.save_file(ArtifactKind.DOWNLOAD, name, source)
                except OSError as exc:
                    raise StepError(
                        ErrorCode.ARTEFATO_FALHOU,
                        f"Não consegui guardar o arquivo baixado no passo '{step.title}'.",
                        details={"detalhe": type(exc).__name__},
                    ) from None
            finally:
                try:
                    await download.delete()
                except PlaywrightError:
                    pass
            await self._register_artifact(step.id, ref)
            # A própria referência: na saída, só o hash dela fica fora da máscara.
            self._vars[step.into] = ref
            return {"arquivo": ref.name, "tamanho": ref.size, "sha256": ref.sha256}

        return act

    def _artifact_name(self, rendered: str, *, default: str) -> str:
        """Nome saneado e sem segredos: vira o nome e o caminho do artefato."""
        name = sanitize_filename(self._redactor.text(rendered), default=default)
        # O saneamento tira acentos e separadores e poderia remontar um segredo.
        return name if self._redactor.text(name) == name else default

    async def _prepare_upload(self, step: UploadStep, ctx: _StepCtx) -> _Action:
        file_id = self._inputs.get(step.file_input)
        if not isinstance(file_id, str) or not file_id:
            raise StepError(
                ErrorCode.ARQUIVO_INDISPONIVEL,
                f"A entrada '{step.file_input}' não traz um arquivo para o passo '{step.title}'.",
            )
        try:
            path = await self._files.path_for(file_id)
        except FileUnavailable as exc:
            raise StepError(
                ErrorCode.ARQUIVO_INDISPONIVEL,
                f"O arquivo da entrada '{step.file_input}' não está disponível: {exc}",
            ) from None
        locator = await self._resolve(step.target, step, ctx)

        async def act() -> dict[str, Any]:
            await self._act_on(step, ctx, locator, lambda on, kw: on.set_input_files(path, **kw))
            size = await asyncio.to_thread(os.path.getsize, path)
            return {"tamanho": size}

        return act

    # ------------------------------------------------------------------
    # Templates, localizadores e condições
    # ------------------------------------------------------------------

    def _render(self, template: str, *, encode: Callable[[int, str], str] | None = None) -> str:
        scopes: dict[templates.Scope, Mapping[str, object]] = {
            templates.Scope.INPUTS: self._inputs,
            templates.Scope.VARS: self._vars,
        }
        if self._items:
            scopes[templates.Scope.ITEM] = self._items[-1][0]
        try:
            return templates.render(template, scopes, encode=encode)
        except templates.TemplateError as exc:
            raise StepError(
                ErrorCode.TEMPLATE_INVALIDO, f"Não consegui montar um valor do passo: {exc}."
            ) from None

    def _build(self, root: _Root, locator: Locator) -> PwLocator:
        value = self._render(locator.value)
        match locator.by:
            case LocatorBy.ROLE:
                role = cast(Any, value)
                if locator.name is None:
                    return root.get_by_role(role)
                return root.get_by_role(role, name=self._render(locator.name), exact=locator.exact)
            case LocatorBy.LABEL:
                return root.get_by_label(value, exact=locator.exact)
            case LocatorBy.PLACEHOLDER:
                return root.get_by_placeholder(value, exact=locator.exact)
            case LocatorBy.TEXT:
                return root.get_by_text(value, exact=locator.exact)
            case LocatorBy.TEST_ID:
                return root.get_by_test_id(value)
            case LocatorBy.CSS:
                return root.locator(f"css={value}")
            case LocatorBy.XPATH:
                return root.locator(f"xpath={value}")
            case _:  # pragma: no cover - conjunto fechado
                assert_never(locator.by)

    def _root(self, target: Target) -> Page | FrameLocator:
        root: Page | FrameLocator = self.page
        for selector in target.frames:
            root = root.frame_locator(selector)
        return root

    def _row_choice(self, target: Target) -> _RowChoice | None:
        """Os contêineres do alvo e os escolhidos pelo texto (``None`` sem esse texto)."""
        self._refuse_dynamic_selectors(target)
        key = self._row_key(target)
        if key is None or target.within is None:
            return None
        rows = self._build(self._root(target), target.within)
        # O texto precisa ser o texto inteiro de um elemento do contêiner (uma célula,
        # por exemplo), com os mesmos caracteres e as mesmas maiúsculas: "12" não
        # escolhe a linha da nota "120" nem a do valor "12,00", e "ab3x" não escolhe a
        # de "AB3X". Mais de um contêiner com o texto continua ambíguo, e o texto
        # mostrado também é conferido (:meth:`_row_issue`).
        return _RowChoice(rows, rows.filter(has=self.page.get_by_text(_whole_text(key))), key)

    def _candidates(self, target: Target, choice: _RowChoice | None) -> list[PwLocator]:
        self._refuse_dynamic_selectors(target)
        root = self._root(target)
        container: _Root = root
        if choice is not None:
            container = choice.chosen
        elif target.within is not None:
            container = self._build(root, target.within)
        return [self._build(container, locator) for locator in target.locators]

    async def _row_issue(self, choice: _RowChoice | None) -> _RowIssue | None:
        """Confere que a linha escolhida pelo texto é a única com ele, nas duas bases.

        Pelo texto do HTML (como o Playwright compara), só um contêiner pode ter um
        elemento com o texto inteiro; pelo texto mostrado (``innerText``,
        normalizado igual), nenhum outro contêiner pode mostrar esse texto, nem o
        que a linha escolhida mostra no lugar dele (a célula ``AB3X`` mostrada como
        ``ab3x`` pelo CSS, ao lado da nota ``ab3x``: a chave pode ter sido lida de
        qualquer uma). Uma célula com ``text-transform`` ou com uma chave de
        ordenação escondida mostra um texto que não está no HTML, e a linha certa
        pode ser outra. ``None`` quando a linha é única (ou o alvo não escolhe linha
        pelo texto); se a página mudar no meio da conferência, a linha não conta
        como única.
        """
        if choice is None:
            return None
        unchecked = _RowIssue(None, None)
        try:
            handles = await choice.chosen.element_handles()
        except PlaywrightError:
            return unchecked
        try:
            if len(handles) != 1:
                return _RowIssue(len(handles), None) if handles else unchecked
            others = await choice.rows.evaluate_all(
                _OTHER_ROWS_SHOWING_KEY_JS, [choice.key, handles[0]]
            )
        except PlaywrightError:
            return unchecked
        finally:
            for handle in handles:
                await self._dispose(handle)
        if not isinstance(others, int) or others < 0:
            return unchecked
        return _RowIssue(1, others) if others else None

    @staticmethod
    def _row_issue_error(target: Target, issue: _RowIssue, details: dict[str, object]) -> StepError:
        if issue.by_html is None:
            reason = "a página mudou enquanto eu conferia se só uma linha tem esse texto"
        elif issue.by_html > 1:
            reason = f"mais de uma linha ({issue.by_html}) tem um elemento com esse texto no HTML"
            details = {**details, "linhas_pelo_html": issue.by_html}
        else:
            reason = (
                "outra linha mostra na página esse mesmo texto, ou o que esta linha mostra "
                "no lugar dele (por exemplo, pelo text-transform do CSS ou por um texto "
                "escondido na célula), e não dá para saber qual delas o texto quer dizer"
            )
            details = {
                **details,
                "linhas_pelo_html": issue.by_html,
                "outras_linhas_pelo_texto_mostrado": issue.shown_elsewhere,
            }
        return StepError(
            ErrorCode.ELEMENTO_AMBIGUO,
            f"O texto que escolhe o contêiner de '{target.intent}' não aponta para uma linha "
            f"só: {reason}. Nada foi feito.",
            details=details,
        )

    def _row_key(self, target: Target) -> str | None:
        """O texto que escolhe o contêiner do alvo, normalizado (``None`` se não houver)."""
        if target.within is None or target.within_has_text is None:
            return None
        # Normalizada como o Playwright normaliza: um texto só de U+200B, U+00AD ou
        # U+FEFF (a célula vazia de um CSV do Excel) fica vazio. Cada valor é
        # conferido sozinho (entradas e itens já foram antes do primeiro passo; aqui
        # entram as variáveis): um valor vazio numa chave com trecho fixo deixaria só
        # o trecho fixo.
        key = _normalize_text(self._render(target.within_has_text, encode=_row_key_value))
        if not key:
            # Sem texto, qualquer elemento vazio (uma célula, um botão só com ícone)
            # serviria, e qualquer contêiner seria escolhido.
            raise StepError(
                ErrorCode.TEMPLATE_INVALIDO,
                f"O texto que escolhe o contêiner de '{target.intent}' ficou vazio; "
                "sem ele não há como saber qual usar.",
            )
        return key

    def _row_not_acted_on(self, step: Step) -> _Act | None:
        """A ação que um passo que grava vai fazer numa linha, se ela não repete outra.

        Rede de segurança contra ações repetidas: numa execução, duas ações que gravam
        (``write`` ou ``irreversible``) nunca caem na mesma linha em voltas diferentes
        de uma repetição (:func:`_iteration_depth`). A linha é a página
        (:func:`_page_key`) e o texto normalizado que a escolhe. As páginas de uma
        ação, para as voltas seguintes de uma repetição em volta dela, são a página
        onde ela agiu e todos os endereços por onde a página principal passou depois,
        na volta dessa repetição em que ela agiu e, depois dela, até o fim dessa
        repetição, menos os que o passo ``navigate`` do robô abre (:meth:`_run_leaf`):
        a resposta de um formulário, enviado por ela ou por um passo seguinte da
        mesma volta (a confirmação de um modal, "Cancelar selecionadas"), que mostra
        a mesma tabela em outro endereço ainda é a mesma linha, e também a página que
        a própria volta abre depois de agir (um link, um ``navigate``) e a que um
        temporizador ou um popup dela abre depois. Uma página que o robô abre com um
        ``navigate`` numa volta seguinte, antes de agir nela, é outra página (a nota
        1 de cada filial); uma aberta por um clique, não (na dúvida, a navegação veio
        da ação anterior). Isso pega itens repetidos numa tabela lida da página,
        inclusive quando cada um vai para um ramo de um ``if``, por qualquer
        localizador, e chaves cruzadas (o segundo passo do item 0 e o primeiro do item
        1 na mesma linha). A segunda vez falha antes de agir, e antes do aviso de passo
        irreversível. Passos de leitura, passos na mesma volta (dois cliques no "+" da
        mesma linha, digitar a quantidade e clicar em Salvar) e repetições que não
        estão uma dentro da outra (marcar todas as linhas, depois cancelar todas) não
        contam; por isso uma ação fora de repetições nunca é anotada. O efeito é o
        de :meth:`_effect`: um clique de leitura num bloco que grava também conta.
        """
        if self._effect(step) is Effect.READ:
            return None
        target = getattr(step, "target", None)
        if not isinstance(target, Target):
            return None
        key = self._row_key(target)
        path = self._iteration_path()
        if key is None or not path:
            return None
        page = _page_key(self.page.url)
        for previous in self._acts.get(key, ()):
            depth = _iteration_depth(previous.path, path)
            if depth is None:
                continue
            # As páginas da volta, daquela repetição, em que a ação anterior agiu. Essa
            # volta já acabou (esta é outra volta da mesma repetição, que ainda roda):
            # cada navegação da página principal fora do ``navigate`` do robô depois da
            # ação também é página dela, e a última de cada página está no mapa da
            # execução inteira (o mesmo que anotar cada uma em cada volta que acabou).
            pages = self._visits.get(previous.path[: depth + 1], {})
            seen = max(pages.get(page, 0), self._navigated.get(page, 0))
            if seen < previous.order:
                continue  # a volta dela não passou por esta página depois de agir
            loop, index = previous.path[depth]
            who = "ele" if previous.step_id == step.id else f"o passo '{previous.step_id}'"
            raise StepError(
                ErrorCode.ELEMENTO_AMBIGUO,
                f"O passo '{step.title}' agiria em '{target.intent}' na linha escolhida por "
                f"este mesmo texto, nesta mesma página, onde {who} já agiu nesta execução, "
                f"na volta nº {index + 1} da repetição '{loop}'; agir de novo gravaria outra "
                "vez na mesma linha, então nada foi feito agora. Confira se a lista tem "
                "itens repetidos.",
                details={
                    "alvo": target.intent,
                    "passo_anterior": previous.step_id,
                    "repeticao": loop,
                    "iteracao_anterior": index,
                },
            )
        return _Act(step.id, key, path)

    async def _confirm_target(
        self, step: Step, ctx: _StepCtx, page: str, act: _Act | None, *, again: bool
    ) -> _Act | None:
        """Confere, logo antes de agir, que um passo que grava age no alvo conferido.

        Depois da espera pela confirmação do aviso de passo irreversível (``again``), a
        página pode ter mudado: o alvo é procurado e conferido de novo (:meth:`_resolve`,
        inclusive a linha única nas duas bases), na mesma página (:func:`_page_key`) da
        primeira conferência, e a rede contra ações repetidas também
        (:meth:`_row_not_acted_on`). Depois, o elemento encontrado fica preso
        (``ctx.element``): a ação vai para ele, e não para o que o localizador encontrar
        na hora (o do Playwright procura de novo, no documento que estiver carregado,
        enquanto espera o elemento ficar habilitado, e cairia na mesma linha de outra
        página).

        A espera pelo elemento (visível, habilitado, parado; editável para digitar; a
        opção na lista) vem antes da conferência da hora de agir, sem mandar nenhuma
        entrada à página (:meth:`_wait_ready`): enquanto o botão espera ficar habilitado,
        a página pode mudar de endereço no mesmo documento (``pushState``, ``#``) ou trocar
        o texto da linha sem trocar os nós. Se o elemento sai da página nessa espera sem
        nenhuma navegação (a página montou o botão de novo), tudo é conferido de novo, uma
        vez; na segunda, o passo falha. Depois da espera, os campos digitados são
        conferidos (:meth:`_typed_fields_hold`) e, com o vigia já na página
        (:meth:`_arm_guard`), o contador de navegações da página principal, a página e a
        linha; a ação vai em seguida (sem ``force``: o Playwright confere que o alvo não
        está coberto nem inerte, também por outro documento), e o vigia confere o
        primeiro evento dela (:meth:`_dispatched`). Se a página principal mudou de endereço
        ou de documento desde a última conferência, o passo falha com
        ``elemento_ambiguo`` e nada é feito. Um segredo já vai para o elemento em que a
        origem foi conferida (:meth:`_prepare_fill`).
        """
        target = getattr(step, "target", None)
        if not isinstance(target, Target) or ctx.locator is None:
            # ``press`` sem alvo: a tecla vai para a página (e pode enviar o formulário).
            ctx.held = _held(await self._typed_fields_hold(step, None))
            return act
        if again:
            await self._resolve(target, step, ctx)
            if _page_key(self.page.url) != page:
                raise self._page_changed(step, target)
            act = self._row_not_acted_on(step)
        if isinstance(step, FillStep) and isinstance(step.value, SecretValue):
            self._raise_if_page_changed(step, target, ctx, page)
            await self._typed_fields_hold(step, None)
            return act
        retried = False
        while True:
            ctx.element = await self._element_handle(ctx.locator, ctx)
            self._raise_if_page_changed(step, target, ctx, page)
            try:
                await self._wait_ready(step, ctx, ctx.element)
                break
            except PlaywrightError:
                if (
                    retried
                    or self._main_frame_changes != ctx.checked_at
                    or await self._attached(ctx.element)
                ):
                    raise
            # O elemento saiu da página antes de qualquer entrada, sem navegação (a página
            # montou o botão de novo): tudo de novo, uma vez.
            retried = True
            await self._dispose(ctx.element)
            ctx.element = None
            checked_at = ctx.checked_at
            await self._resolve(target, step, ctx)
            if ctx.checked_at != checked_at or _page_key(self.page.url) != page:
                raise self._page_changed(step, target)
            act = self._row_not_acted_on(step)
        frame = await self._frame_of(ctx.element) if self._typed else None
        elsewhere = any(frame is None or entry.frame is not frame for entry in self._typed)
        points = await self._points_at(step, ctx.element)
        if points and (elsewhere or await self._in_transformed_frame(ctx.element)):
            # Há campos digitados noutro documento (o vigia não os lê no primeiro evento: a
            # espera pelo alvo livre vem antes da releitura), ou o alvo está num iframe
            # transformado, onde o Playwright não confere o que está por cima dele.
            await self._wait_uncovered(step, ctx, ctx.element, elsewhere=elsewhere)
        typed = await self._typed_fields_hold(
            step, ctx.element if isinstance(step, FillStep) else None
        )
        ctx.held = _held(typed)
        await self._arm_guard(step, ctx, target, page, typed, frame)
        if points or await self._points_at(step, ctx.element):
            # A página pode ter reaproveitado o nó para outro item depois de ele ser preso (a
            # lista virtual que redesenha no quadro seguinte à rolagem da espera, o feed que
            # põe um item no topo): com o vigia já posto, o elemento preso ainda precisa ser
            # o único do localizador. O que a página redesenha antes do vigia falha aqui; o
            # que ela redesenha depois troca o rótulo que o vigia guardou. Numa marcação, se a
            # caixa já estava como devia, a pergunta é feita de novo: o nó reaproveitado pode
            # mostrar um item no outro estado, e a marcação clicaria nele.
            await self._still_the_only_one(step, ctx, target)
        return act

    async def _still_the_only_one(self, step: Step, ctx: _StepCtx, target: Target) -> None:
        """O elemento preso ainda é o único que o localizador encontra; senão, o passo falha
        com ``elemento_ambiguo`` sem agir."""
        element = ctx.element
        if ctx.locator is None or element is None:  # pragma: no cover - preso antes
            return
        try:
            handles = await ctx.locator.element_handles()
        except PlaywrightError:
            handles = []
        try:
            same = await self._same_elements(element, handles)
        finally:
            for handle in handles:
                await self._dispose(handle)
        if same != [True]:
            raise self._page_changed(step, target)

    def _raise_if_page_changed(self, step: Step, target: Target, ctx: _StepCtx, page: str) -> None:
        """A página principal mudou de endereço ou de documento desde a conferência do alvo."""
        if self._main_frame_changes != ctx.checked_at or _page_key(self.page.url) != page:
            raise self._page_changed(step, target)

    @staticmethod
    async def _attached(handle: ElementHandle) -> bool:
        try:
            return await handle.evaluate("(element) => element.isConnected") is True
        except PlaywrightError:
            return False

    async def _wait_ready(self, step: Step, ctx: _StepCtx, element: ElementHandle) -> None:
        """Espera o que o Playwright esperaria antes de agir, sem mandar nenhuma entrada.

        Clicar, marcar e baixar: visível, habilitado e parado; digitar: visível e
        editável; escolher: visível, habilitado e com a opção na lista; teclar e enviar um
        arquivo não esperam nada. Não usa o clique de teste (``trial``) do Playwright, que
        manda os eventos do mouse à página e só os barra num ouvinte dele.
        """
        states: tuple[Literal["visible", "enabled", "editable", "stable"], ...]
        if isinstance(step, (ClickStep, DownloadStep)):
            states = ("visible", "enabled", "stable")
        elif isinstance(step, CheckStep):
            if await element.is_checked() == step.checked:
                return  # já está como deve ficar: o Playwright não clica
            states = ("visible", "enabled", "stable")
        elif isinstance(step, FillStep):
            states = ("visible", "editable")
        elif isinstance(step, SelectStep):
            states = ("visible", "enabled")
        else:
            return
        for state in states:
            await element.wait_for_element_state(state, timeout=ctx.remaining_ms())
        if isinstance(step, SelectStep):
            option = self._render(step.option)
            loop = asyncio.get_running_loop()
            while not await element.evaluate(_HAS_OPTION_JS, option):
                if loop.time() + self._poll_s >= ctx.deadline:
                    return  # a própria ação espera o resto do prazo e falha
                await asyncio.sleep(self._poll_s)

    async def _arm_guard(
        self,
        step: Step,
        ctx: _StepCtx,
        target: Target,
        page: str,
        typed: list[tuple[_Typed, str]],
        frame: Frame | None,
    ) -> None:
        """Põe o vigia (:data:`_DISPATCH_GUARD_JS`) e faz a conferência da hora de agir.

        Com o vigia já na página, o contador de navegações da página principal e a página
        são os da conferência do alvo e, numa linha escolhida pelo texto, ela ainda é a
        única com ele (:meth:`_row_issue`) e o elemento conferido ainda está nela. Senão, o
        passo falha com ``elemento_ambiguo`` sem agir. Num clique, numa marcação ou numa
        escolha, o vigia também leva os campos digitados do mesmo documento do alvo, com o
        valor que :meth:`_typed_fields_hold` leu: se a página muda algum enquanto o
        Playwright espera o alvo ficar livre (uma camada de "buscando CEP"), o primeiro
        evento da ação é cancelado e o passo falha com ``acao_falhou``. Os campos digitados
        de outro documento (``frame`` é o do alvo) foram relidos depois de o alvo ficar
        livre (:meth:`_wait_uncovered`); se, mesmo assim, o primeiro evento chega mais de
        :data:`_LATE_EVENT_MS` depois do vigia (o Playwright esperou algo que a espera não
        viu, como um documento de outra origem por cima do alvo, ou uma camada que chegou
        depois dela), ele é cancelado e o passo falha com ``acao_falhou``.
        """
        element = ctx.element
        if element is None:  # pragma: no cover - preso em _confirm_target
            raise AssertionError("o elemento conferido não foi preso")
        choice = self._row_choice(target)
        row: ElementHandle | None = None
        try:
            if choice is not None:
                rows = await choice.chosen.element_handles()
                if len(rows) != 1:
                    for handle in rows:
                        await self._dispose(handle)
                    raise self._page_changed(step, target)
                row = rows[0]
            if isinstance(step, (ClickStep, DownloadStep)):
                kind = "clique"
            elif isinstance(step, CheckStep):
                kind = "marca"
            elif isinstance(step, FillStep):
                kind = "texto"
            elif isinstance(step, PressStep):
                kind = "tecla"
            else:
                kind = "campo"
            # Digitar e teclar põem o foco no alvo antes do primeiro evento (o campo de
            # antes perde o foco, e a página pode formatá-lo): o vigia só confere os campos
            # digitados nos cliques, marcações e escolhas, cujo primeiro evento vem antes de
            # o foco mudar.
            same: list[list[Any]] = []
            late: float | None = None
            if kind in ("clique", "marca", "campo"):
                for entry, value in typed:
                    if frame is not None and entry.frame is frame:
                        same.append([entry.handle, value])
                    else:
                        late = _LATE_EVENT_MS
            ctx.guard = await element.evaluate_handle(
                _DISPATCH_GUARD_JS,
                [kind, row, choice.key if choice is not None else None, same, late],
            )
            self._raise_if_page_changed(step, target, ctx, page)
            if choice is not None and row is not None:
                try:
                    others = await choice.rows.evaluate_all(
                        _PINNED_ROW_JS, [choice.key, row, element]
                    )
                except PlaywrightError:
                    others = None
                if others != 0:
                    raise self._page_changed(step, target)
        finally:
            if row is not None:
                await self._dispose(row)

    @staticmethod
    async def _frame_of(element: ElementHandle) -> Frame | None:
        try:
            return await element.owner_frame()
        except PlaywrightError:
            return None

    @staticmethod
    async def _points_at(step: Step, element: ElementHandle) -> bool:
        """Se a ação do passo é um clique do mouse no elemento (que o Playwright só manda
        com o alvo livre): um clique, um download ou uma marcação que muda a caixa."""
        if isinstance(step, (ClickStep, DownloadStep)):
            return True
        if isinstance(step, CheckStep):
            try:
                return await element.is_checked() != step.checked
            except PlaywrightError:
                return True
        return False

    @staticmethod
    async def _in_transformed_frame(element: ElementHandle) -> bool:
        """O elemento está num iframe deslocado, em escala ou girado por uma transformação
        (dele ou de um elemento em volta dele, num documento da mesma origem acima)."""
        try:
            return await element.evaluate(_IN_TRANSFORMED_FRAME_JS) is True
        except PlaywrightError:
            return False

    async def _wait_uncovered(
        self, step: Step, ctx: _StepCtx, element: ElementHandle, *, elsewhere: bool
    ) -> None:
        """Espera, só lendo a página, que um clique no centro do elemento chegue a ele
        (:data:`_UNCOVERED_JS`: nada do documento dele, nem dos documentos da mesma origem
        acima dele, por cima), como o Playwright esperaria antes de clicar.

        Com campos digitados noutro documento (``elsewhere``), vem antes da releitura
        deles: o que a página muda nesses campos enquanto uma camada de "buscando CEP"
        cobre o alvo é visto pela releitura, depois. Num iframe transformado, o Playwright
        não confere o que está por cima do alvo (clicaria no aviso da página por cima do
        iframe): a espera confere por ele. Como o Playwright, rola o elemento para a janela de novo a
        cada tentativa (no fim, no centro, no começo: um cabeçalho fixo por cima dele). Se
        o alvo continua coberto quando falta menos de :data:`_UNCOVERED_MARGIN_S` do prazo
        do passo, o passo falha sem agir (``tempo_do_passo_esgotado``). Num iframe girado
        ou inclinado, onde o ponto não se traduz, deixa a ação para o Playwright se não há
        campos digitados noutro documento; se há, espera como por uma camada (uma animação
        de entrada que gira o modal) e, no fim do prazo, falha sem agir (``acao_falhou``).
        Se o elemento sai da página, a própria ação decide. A página pode ter reaproveitado
        o nó na rolagem: :meth:`_confirm_target` confere de novo, com o vigia já posto, que
        o elemento preso é o único que o localizador encontra.
        """
        loop = asyncio.get_running_loop()
        aligns = ("end", "center", "start")
        tries = 0
        target = getattr(step, "target", None)
        intent = target.intent if isinstance(target, Target) else step.title
        try:
            await element.scroll_into_view_if_needed(timeout=ctx.remaining_ms())
            while not (free := await element.evaluate(_UNCOVERED_JS)):
                if free is None and not elsewhere:
                    return  # sem campos digitados noutro documento: como o Playwright
                if (
                    free is None
                    and loop.time() + self._poll_s + _UNCOVERED_MARGIN_S >= ctx.deadline
                ):
                    raise StepError(
                        ErrorCode.ACAO_FALHOU,
                        f"O alvo do passo '{step.title}' está num iframe girado, inclinado ou "
                        "em perspectiva, onde não dá para conferir que o clique chega a ele, e "
                        "há campos digitados noutro documento. Nada foi feito.",
                        details={"alvo": intent},
                    )
                if loop.time() + self._poll_s + _UNCOVERED_MARGIN_S >= ctx.deadline:
                    raise StepError(
                        ErrorCode.TEMPO_DO_PASSO_ESGOTADO,
                        f"O alvo do passo '{step.title}' ficou coberto por outro elemento "
                        "até o fim do prazo do passo, e nada foi feito.",
                        details={"alvo": intent},
                    )
                await asyncio.sleep(self._poll_s)
                await element.evaluate(_SCROLL_INTO_VIEW_JS, aligns[tries % len(aligns)])
                tries += 1
        except PlaywrightError:
            return

    async def _act_on(
        self,
        step: Step,
        ctx: _StepCtx,
        locator: PwLocator,
        action: Callable[[PwLocator | ElementHandle, dict[str, Any]], Awaitable[object]],
    ) -> None:
        """Faz ``action`` no localizador (num passo de leitura) ou no elemento conferido.

        No elemento conferido, a espera já foi feita antes da conferência da hora de agir
        (:meth:`_confirm_target`). A ação vai sem ``force``: o Playwright ainda confere,
        sem esperar de novo o que já esperou, que o elemento não está coberto (também por
        outro documento, como um iframe por cima dele) nem inerte, e o vigia confere os
        eventos dela (:meth:`_dispatched`).
        O prazo continua o do passo: o Playwright ainda espera, depois de agir, a
        navegação que a ação começou.
        """
        if ctx.element is None:
            await action(locator, {"timeout": ctx.remaining_ms()})
            return
        options: dict[str, Any] = {"timeout": ctx.remaining_ms()}
        await self._dispatched(step, ctx, action(ctx.element, options))

    async def _dispatched(self, step: Step, ctx: _StepCtx, action: Awaitable[object]) -> None:
        """Age e lê o vigia: se ele recusou o primeiro evento da ação (outro endereço, outro
        elemento, a linha ou o rótulo mudou), o evento foi cancelado e o passo falha com
        ``elemento_ambiguo``; se um campo digitado mudou, ou pode ter mudado sem ser visto
        (o primeiro evento demorou, com campos digitados noutro documento), com
        ``acao_falhou``. Um clique que o navegador entregou a outro documento por cima do
        alvo (``fora``) falha com ``elemento_ambiguo``, dizendo que ele pode ter feito
        outra coisa; um clique que o vigia interrompeu depois do primeiro evento, porque a
        página mudou o endereço, a linha ou o rótulo com os eventos de antes (``meio``),
        também, dizendo que esses eventos chegaram à página."""
        failure: Exception | None = None
        try:
            await action
        except Exception as exc:
            failure = exc
        reason = await self._guard_refused(ctx)
        target = getattr(step, "target", None)
        if reason == "campo":
            raise StepError(
                ErrorCode.ACAO_FALHOU,
                "Um campo que o robô digitou mudou enquanto o passo "
                f"'{step.title}' esperava o alvo ficar livre: ele enviaria um valor que não "
                "foi digitado, então nada foi feito.",
                details={"alvo": target.intent if isinstance(target, Target) else step.title},
            ) from None
        if reason == "espera":
            raise StepError(
                ErrorCode.ACAO_FALHOU,
                f"O passo '{step.title}' esperou o alvo ficar livre, e um campo que o robô "
                "digitou noutro documento (um iframe) pode ter mudado nessa espera sem ser "
                "visto: ele enviaria um valor que não foi digitado, então nada foi feito.",
                details={"alvo": target.intent if isinstance(target, Target) else step.title},
            ) from None
        if reason == "meio" and isinstance(target, Target):
            raise StepError(
                ErrorCode.ELEMENTO_AMBIGUO,
                f"A página mudou durante o clique do passo '{step.title}' (o endereço, a linha ou o "
                f"rótulo de '{target.intent}'), e o robô o interrompeu antes do fim: os "
                "primeiros eventos do clique já tinham chegado à página. Confira o sistema de "
                "destino antes de repetir.",
                details={"alvo": target.intent},
            ) from None
        if reason == "fora" and failure is None and isinstance(target, Target):
            raise StepError(
                ErrorCode.ELEMENTO_AMBIGUO,
                f"O clique do passo '{step.title}' não chegou a '{target.intent}': o navegador "
                "o entregou a outro documento por cima dele (a página por cima de um iframe), "
                "onde ele pode ter feito outra coisa. Confira o sistema de destino antes de "
                "repetir.",
                details={"alvo": target.intent},
            ) from None
        if reason is not None and isinstance(target, Target):
            raise self._page_changed(step, target) from None
        if failure is not None:
            raise failure

    async def _guard_refused(self, ctx: _StepCtx) -> str | None:
        """Tira o vigia da página e diz por que ele recusou um evento (``alvo``, ``campo``,
        ``espera`` ou ``meio``), se o clique não chegou à janela do alvo (``fora``), ou
        ``None``.

        Se o documento já foi trocado (a própria ação navegou), não há o que ler, e vale o
        resultado da ação: um evento recusado é cancelado e não leva a página a outro
        documento.
        """
        guard, ctx.guard = ctx.guard, None
        if guard is None:
            return None
        try:
            reason = await guard.evaluate("(guard) => guard.stop()")
            return reason if isinstance(reason, str) else None
        except PlaywrightError:
            return None
        finally:
            await self._dispose(guard)

    @staticmethod
    def _page_changed(step: Step, target: Target) -> StepError:
        return StepError(
            ErrorCode.ELEMENTO_AMBIGUO,
            f"A página mudou entre a conferência de '{target.intent}' e a ação do passo "
            f"'{step.title}', e não dá para saber se o alvo ainda é o mesmo. Nada foi feito.",
            details={"alvo": target.intent},
        )

    def _on(self, ctx: _StepCtx, locator: PwLocator) -> PwLocator | ElementHandle:
        """Onde o passo age: no elemento conferido (num passo que grava) ou no localizador."""
        return locator if ctx.element is None else ctx.element

    @staticmethod
    def _refuse_dynamic_selectors(target: Target) -> None:
        """Seletores CSS/XPath não escapam valores: um valor com aspas ou colchetes
        escolheria outro elemento. Vale também para robôs publicados antes de o
        portão de política recusar isso."""
        locators = [*target.locators, *([target.within] if target.within is not None else [])]
        for locator in locators:
            if locator.by in RAW_SELECTORS and _has_references(locator.value):
                raise StepError(
                    ErrorCode.TEMPLATE_INVALIDO,
                    f"O alvo '{target.intent}' usa uma entrada, variável ou item num "
                    f"localizador {locator.by.value}, onde o valor entraria cru no seletor. "
                    "O robô precisa de conserto.",
                    details={"estrategia": locator.by.value},
                )

    @staticmethod
    async def _count(locator: PwLocator) -> int:
        try:
            return await locator.count()
        except PlaywrightError:
            # A página pode estar no meio de uma navegação.
            return 0

    async def _resolve(self, target: Target, step: Step, ctx: _StepCtx) -> PwLocator:
        """Encontra o elemento-alvo, que precisa ser único.

        O localizador principal tem preferência. Em passos de leitura, os
        alternativos entram depois de ``heal_after_ms`` (ou no fim do prazo) e
        geram o evento ``step_healed``; em passos que gravam (pelo efeito de
        :meth:`_effect`, também o de um bloco que grava), só o principal vale.
        Num contêiner escolhido pelo texto, a linha também precisa ser a única com
        ele, no HTML e no texto mostrado (:meth:`_row_issue`).
        """
        choice = self._row_choice(target)
        candidates = self._candidates(target, choice)
        allow_fallback = self._effect(step) is Effect.READ
        loop = asyncio.get_running_loop()
        start = loop.time()
        heal_at = start + min(self._heal_after_s, max(0.0, (ctx.deadline - start) / 4))
        counts: list[int | None] = [None] * len(candidates)
        while True:
            last_round = loop.time() + self._poll_s >= ctx.deadline
            issue: _RowIssue | None = None
            found: int | None = None
            checked_at = self._main_frame_changes
            counts[0] = await self._count(candidates[0])
            if counts[0] == 1:
                found = 0
            elif allow_fallback and (last_round or loop.time() >= heal_at):
                for index in range(1, len(candidates)):
                    counts[index] = await self._count(candidates[index])
                    if counts[index] == 1:
                        found = index
                        break
            if found is not None:
                # O contêiner é o mesmo para todos os localizadores.
                issue = await self._row_issue(choice)
                if issue is None:
                    if found:
                        await self._emit(
                            RunEventKind.STEP_HEALED,
                            step.id,
                            {
                                "localizador": found,
                                "estrategia": target.locators[found].by.value,
                                "contagens": list(counts),
                            },
                        )
                    ctx.locator, ctx.checked_at = candidates[found], checked_at
                    return candidates[found]
            if last_round:
                break
            await asyncio.sleep(self._poll_s)

        self._raise_if_run_deadline(ctx)
        for index in range(1, len(candidates)):
            if counts[index] is None:
                counts[index] = await self._count(candidates[index])
        details: dict[str, object] = {
            "alvo": target.intent,
            "estrategias": [locator.by.value for locator in target.locators],
            "contagens": list(counts),
        }
        if issue is not None:
            raise self._row_issue_error(target, issue, details)
        if not allow_fallback:
            fallback = next((i for i in range(1, len(counts)) if counts[i] == 1), None)
            if fallback is not None:
                raise StepError(
                    ErrorCode.LOCALIZADOR_ALTERNATIVO_EM_GRAVACAO,
                    f"O elemento '{target.intent}' só foi encontrado pelo localizador "
                    f"alternativo nº {fallback + 1}; em passos que gravam, só o localizador "
                    "principal pode ser usado. O robô precisa de conserto.",
                    details={**details, "localizador": fallback},
                )
        considered = counts if allow_fallback else counts[:1]
        most = max((count or 0) for count in considered)
        if most > 1:
            raise StepError(
                ErrorCode.ELEMENTO_AMBIGUO,
                f"O elemento '{target.intent}' corresponde a {most} elementos na página; "
                "o localizador precisa encontrar exatamente um.",
                details=details,
            )
        raise StepError(
            ErrorCode.ELEMENTO_NAO_ENCONTRADO,
            f"Não encontrei o elemento '{target.intent}' no passo '{step.title}'.",
            details=details,
        )

    async def _element_handle(self, locator: PwLocator, ctx: _StepCtx) -> ElementHandle:
        return await locator.element_handle(timeout=ctx.remaining_ms())

    async def _owner_frame(self, locator: PwLocator, ctx: _StepCtx) -> Frame | None:
        handle = await self._element_handle(locator, ctx)
        try:
            return await handle.owner_frame()
        finally:
            await self._dispose(handle)

    @staticmethod
    async def _dispose(handle: JSHandle) -> None:
        try:
            await handle.dispose()
        except PlaywrightError:
            pass

    async def _wait_hidden(self, target: Target, ctx: _StepCtx) -> None:
        candidates = self._candidates(target, self._row_choice(target))
        loop = asyncio.get_running_loop()
        while True:
            visible = 0
            for candidate in candidates:
                visible += await self._count(candidate.filter(visible=True))
            if visible == 0:
                return
            if loop.time() + self._poll_s >= ctx.deadline:
                self._raise_if_run_deadline(ctx)
                raise StepError(
                    ErrorCode.TEMPO_DO_PASSO_ESGOTADO,
                    f"O elemento '{target.intent}' continuou visível no passo '{ctx.step.title}'.",
                )
            await asyncio.sleep(self._poll_s)

    async def _first_unique(self, target: Target) -> PwLocator | None:
        """O primeiro localizador com um elemento só (``None`` se nenhum tem).

        Num contêiner escolhido pelo texto, se alguma linha tem o texto no HTML, ela
        precisa ser a única com ele, nas duas bases (como em :meth:`_resolve`); senão
        a condição falha fechado com ``elemento_ambiguo``, em vez de contar como não
        encontrada: "se a linha da nota não está visível, crie a nota" nunca grava
        porque a linha certa não se distingue de outra.
        """
        choice = self._row_choice(target)
        if choice is not None and await self._count(choice.chosen) > 0:
            issue = await self._row_issue(choice)
            if issue is not None:
                raise self._row_issue_error(target, issue, {"alvo": target.intent})
        for candidate in self._candidates(target, choice):
            if await self._count(candidate) == 1:
                return candidate
        return None

    async def _holds(self, condition: Condition) -> bool:
        """Avalia a condição uma vez. Erros do navegador contam como 'falso'.

        Uma linha ambígua (:meth:`_first_unique`) nunca conta como 'falso': levanta
        :class:`~rapa.runner.errors.StepError` com ``elemento_ambiguo``.
        """
        try:
            match condition:
                case VisibleCondition(target=target):
                    found = await self._first_unique(target)
                    result = found is not None and await found.is_visible()
                case TextContainsCondition(target=target, text=text):
                    needle = self._render(text)
                    if target is None:
                        haystack = await self.page.locator("body").inner_text(timeout=2_000)
                    else:
                        found = await self._first_unique(target)
                        haystack = await found.inner_text(timeout=2_000) if found else ""
                    result = needle in haystack
                case UrlContainsCondition(value=value):
                    result = self._render(value) in self.page.url
                case EqualsCondition(left=left, right=right):
                    result = self._render(left) == self._render(right)
                case _:  # pragma: no cover - conjunto fechado
                    assert_never(condition)
        except PlaywrightError:
            result = False
        return result != condition.negate

    async def _wait_condition(self, condition: Condition, deadline: float) -> bool:
        """Espera a condição até ``deadline``.

        Uma linha ambígua pode deixar de ser enquanto a página carrega: a espera
        continua, e só se ela ainda for ambígua no fim o erro (``elemento_ambiguo``)
        sobe.
        """
        loop = asyncio.get_running_loop()
        while True:
            ambiguous: StepError | None = None
            try:
                if await self._holds(condition):
                    return True
            except StepError as err:
                if err.code != ErrorCode.ELEMENTO_AMBIGUO:
                    raise
                ambiguous = err
            if loop.time() + self._poll_s >= deadline:
                if ambiguous is not None:
                    raise ambiguous
                return False
            await asyncio.sleep(self._poll_s)

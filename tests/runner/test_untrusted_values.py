"""Valores que vêm de quem chama o robô ou da página nunca mudam o alvo de um passo
nem escapam da máscara de segredos.

As páginas são servidas pelo próprio navegador (``route.fulfill``), num domínio
de teste que não precisa de servidor.
"""

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from playwright.async_api import BrowserContext, Route

from rapa.core import policy
from rapa.core.contracts import RunMode, RunState
from rapa.core.events import RunEventKind
from rapa.core.robot import Manifest
from rapa.runner.errors import ErrorCode
from rapa.runner.interpreter import MASK
from rapa.runner.ports import AckingEventSink, ListEventSink, ack_path, needs_ack
from rapa.runner.result import ArtifactKind
from tests.runner.support import ContextSetup, RobotRunner, assert_seq

pytestmark = pytest.mark.browser

HOST = "paginas.rapa.test"
BASE = f"http://{HOST}"


def _target(intent: str, *locators: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"intent": intent, "locators": list(locators), **extra}


def _manifest(steps: list[dict[str, Any]], **extra: Any) -> Manifest:
    return Manifest.model_validate(
        {
            "name": "robo_de_teste",
            "title": "Robô de teste",
            "description": "Robô montado pelos testes de valores não confiáveis.",
            "allowed_domains": [HOST],
            "steps": steps,
            **extra,
        }
    )


def _open(path: str, step_id: str = "abrir") -> dict[str, Any]:
    return {"kind": "navigate", "id": step_id, "title": "Abrir a página", "url": f"{BASE}{path}"}


def _list_input(name: str, *fields: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "title": name, "type": "list", "item_fields": list(fields)}


def _serve(
    pages: dict[str, str],
    *,
    requests: list[str] | None = None,
    downloads: dict[str, bytes] | None = None,
) -> ContextSetup:
    """Responde a todas as requisições do contexto com as páginas dadas, por caminho."""

    async def prepare(context: BrowserContext) -> None:
        async def handler(route: Route) -> None:
            url = route.request.url
            if requests is not None:
                requests.append(url)
            path = urlsplit(url).path
            if downloads is not None and path in downloads:
                await route.fulfill(
                    body=downloads[path],
                    content_type="application/octet-stream",
                    headers={"content-disposition": 'attachment; filename="arquivo.bin"'},
                )
                return
            body = pages.get(path, "<!doctype html><title>Outra</title><p>outra página</p>")
            await route.fulfill(content_type="text/html; charset=utf-8", body=body)

        await context.route("**/*", handler)

    return prepare


class _Page:
    """Lê algo da página depois da execução (título, valor de um campo...)."""

    def __init__(self) -> None:
        self.title: str | None = None
        self.log: str | None = None
        self.values: dict[str, str] = {}

    async def __call__(self, context: BrowserContext) -> None:
        page = context.pages[0]
        self.title = await page.title()
        log = page.locator("#log")
        if await log.count():
            self.log = await log.inner_text()
        for field in await page.locator("input").all():
            name = await field.get_attribute("name")
            if name:
                self.values[name] = await field.input_value()


# -- linha da tabela: o texto-chave precisa ser o texto inteiro de uma célula -------------

_NOTAS = """<!doctype html><meta charset="utf-8"><title>Notas</title>
<table aria-label="Notas">
  <thead><tr><th>Número</th><th>Valor</th><th>Ação</th></tr></thead>
  <tbody>
    <tr><td>120</td><td>5,00</td>
      <td><button onclick="document.title='cancelou 120'">Cancelar</button></td></tr>
    <tr><td>  7 </td><td>3,00</td>
      <td><button onclick="document.title='cancelou 7'">Cancelar</button></td></tr>
    <tr><td><span>ABC</span></td><td>1,00</td>
      <td><button onclick="document.title='cancelou ABC'">Cancelar</button></td></tr>
  </tbody>
</table>"""

_UMA_NOTA = """<!doctype html><meta charset="utf-8"><title>Notas</title>
<table aria-label="Notas"><tbody>
  <tr><td>120</td><td><button onclick="document.title='cancelou 120'">Cancelar</button></td></tr>
</tbody></table>"""


def _cancelar_notas(path: str = "/notas") -> Manifest:
    return _manifest(
        [
            _open(path),
            {
                "kind": "for_each",
                "id": "cada_nota",
                "title": "Para cada nota",
                "items": "inputs.notas",
                "steps": [
                    {
                        "kind": "click",
                        "id": "cancelar",
                        "title": "Cancelar a nota",
                        "effect": "irreversible",
                        "timeout_ms": 1_000,
                        "target": _target(
                            "botão Cancelar da nota",
                            {"by": "role", "value": "button", "name": "Cancelar"},
                            within={"by": "role", "value": "row"},
                            within_has_text="{{ item.numero }}",
                        ),
                    }
                ],
            },
        ],
        inputs=[_list_input("notas", {"name": "numero", "title": "Número", "type": "string"})],
    )


@pytest.mark.parametrize(
    "numero",
    [
        "12",  # começo de outro número (120)
        "3,0",  # pedaço de outra célula (o valor 3,00)
        "AB",  # pedaço de outra célula, em outra caixa
        "1.00",  # o ponto não vale como "qualquer caractere" (o valor 1,00)
        "12 0",  # o espaço do meio conta
    ],
)
async def test_row_key_must_be_a_whole_cell(run_robot: RobotRunner, numero: str) -> None:
    page = _Page()
    outcome = await run_robot(
        _cancelar_notas(),
        {"notas": [{"numero": numero}]},
        prepare_context=_serve({"/notas": _NOTAS}),
        after_run=page,
    )
    result = outcome.result
    assert result.state is RunState.FAILED, result.error_message
    assert result.error_code == ErrorCode.ELEMENTO_NAO_ENCONTRADO
    assert result.failed_step_id == "cancelar"
    assert not result.irreversible_started
    assert page.title == "Notas"  # nenhuma nota foi cancelada


async def test_row_key_matches_the_exact_cell(run_robot: RobotRunner) -> None:
    page = _Page()
    outcome = await run_robot(
        _cancelar_notas(),
        # A caixa não conta: a chave gravada vem de innerText, que aplica o
        # text-transform do CSS (ver test_row_key_ignores_case_and_spacing...).
        {"notas": [{"numero": "7"}, {"numero": "abc"}]},
        prepare_context=_serve({"/notas": _NOTAS}),
        after_run=page,
    )
    assert outcome.result.state is RunState.SUCCEEDED, outcome.result.error_message
    assert len(outcome.of(RunEventKind.STEP_SUCCEEDED, "cancelar")) == 2
    assert page.title == "cancelou ABC"


async def test_row_key_inside_a_frame(run_robot: RobotRunner) -> None:
    inner = _NOTAS.replace('"', "&quot;").replace("document.title", "parent.document.title")
    framed = f'<!doctype html><title>Notas</title><iframe id="quadro" srcdoc="{inner}"></iframe>'

    data = _cancelar_notas().model_dump(mode="json")
    data["steps"][1]["steps"][0]["target"]["frames"] = ["#quadro"]
    manifest = Manifest.model_validate(data)
    page = _Page()
    outcome = await run_robot(
        manifest,
        {"notas": [{"numero": "7"}, {"numero": "12"}]},
        prepare_context=_serve({"/notas": framed}),
        after_run=page,
    )
    # A nota 7 foi cancelada; "12" não escolheu a linha da 120, e a falha depois de
    # um passo irreversível deixa a execução indeterminada.
    assert outcome.result.state is RunState.INDETERMINATE
    assert outcome.result.error_code == ErrorCode.ELEMENTO_NAO_ENCONTRADO
    assert len(outcome.of(RunEventKind.STEP_SUCCEEDED, "cancelar")) == 1
    assert page.title == "cancelou 7"


_CAIXAS = """<!doctype html><meta charset="utf-8"><title>Notas</title>
<table aria-label="Notas"><tbody>
  <tr><td style="text-transform: uppercase">nf 7</td>
    <td><button onclick="document.title='cancelou 7'">Cancelar</button></td></tr>
  <tr><td>
        NF
        8
      </td>
    <td><button onclick="document.title='cancelou 8'">Cancelar</button></td></tr>
  <tr><td>Nota "A" &gt;&gt; 'B'</td>
    <td><button onclick="document.title='cancelou A'">Cancelar</button></td></tr>
  <tr><td>001.003/2026</td>
    <td><button onclick="document.title='cancelou 1'">Cancelar</button></td></tr>
  <tr><td>001x003/2026</td>
    <td><button onclick="document.title='cancelou x'">Cancelar</button></td></tr>
  <tr><td>Pro&shy;to&#8203;colo</td>
    <td><button onclick="document.title='cancelou protocolo'">Cancelar</button></td></tr>
</tbody></table>"""


@pytest.mark.parametrize(
    ("numero", "title"),
    [
        ("NF 7", "cancelou 7"),  # o que innerText devolve com text-transform: uppercase
        ("nf 8", "cancelou 8"),  # outra caixa e quebras de linha na célula
        ("  NF \t 8 ", "cancelou 8"),
        ("nota \"a\" >> 'b'", "cancelou A"),  # aspas e ">>" não confundem o seletor
        ("001.003/2026", "cancelou 1"),  # o ponto é só um ponto
        ("PROTOCOLO", "cancelou protocolo"),  # U+00AD e U+200B da página não contam
    ],
)
async def test_row_key_ignores_case_and_spacing_but_not_characters(
    run_robot: RobotRunner, numero: str, title: str
) -> None:
    page = _Page()
    outcome = await run_robot(
        _cancelar_notas("/caixas"),
        {"notas": [{"numero": numero}]},
        prepare_context=_serve({"/caixas": _CAIXAS}),
        after_run=page,
    )
    assert outcome.result.state is RunState.SUCCEEDED, outcome.result.error_message
    assert page.title == title


@pytest.mark.parametrize(
    ("de", "para", "numero"),
    [
        ("001x003/2026", "001.003/2026", "001.003/2026"),  # o mesmo texto em duas linhas
        ("nf 7", "NF 8", "nf 8"),  # o mesmo texto, em outra caixa
    ],
)
async def test_row_key_in_two_rows_is_ambiguous(
    run_robot: RobotRunner, de: str, para: str, numero: str
) -> None:
    page = _Page()
    outcome = await run_robot(
        _cancelar_notas("/caixas"),
        {"notas": [{"numero": numero}]},
        prepare_context=_serve({"/caixas": _CAIXAS.replace(de, para)}),
        after_run=page,
    )
    assert outcome.result.state is RunState.FAILED
    assert outcome.result.error_code == ErrorCode.ELEMENTO_AMBIGUO
    assert not outcome.result.irreversible_started
    assert page.title == "Notas"


_COM_CELULA_VAZIA = """<!doctype html><meta charset="utf-8"><title>Notas</title>
<table aria-label="Notas"><tbody>
  <tr><td>120</td><td></td>
    <td><button onclick="document.title='cancelou 120'">Cancelar</button></td></tr>
</tbody></table>"""


@pytest.mark.parametrize(
    "numero",
    [
        "",
        "​",  # espaço de largura zero
        "﻿",  # BOM: a primeira célula vazia de um CSV do Excel
        "­",  # hífen opcional
        " ​﻿   ",
    ],
)
async def test_blank_row_key_fails_before_any_step(run_robot: RobotRunner, numero: str) -> None:
    # Normalizada como o Playwright normaliza, a chave fica vazia e escolheria a
    # linha pela célula vazia. A primeira nota é válida: nada pode ser cancelado
    # antes de a execução chegar à segunda.
    requests: list[str] = []
    outcome = await run_robot(
        _cancelar_notas("/uma"),
        {"notas": [{"numero": "120"}, {"numero": numero}]},
        prepare_context=_serve({"/uma": _COM_CELULA_VAZIA}, requests=requests),
    )
    result = outcome.result
    assert result.state is RunState.FAILED, result.error_message
    assert result.error_code == ErrorCode.ENTRADA_INVALIDA
    assert "'notas[1]'" in (result.error_message or "")
    assert not result.irreversible_started
    assert outcome.kinds() == [RunEventKind.RUN_STARTED, RunEventKind.RUN_FAILED]
    assert requests == []  # nem a página foi aberta


async def test_blank_row_key_from_the_page_is_refused_when_the_step_runs(
    run_robot: RobotRunner,
) -> None:
    pagina = _COM_CELULA_VAZIA.replace("<table", '<p id="chave">&#xFEFF;&#8203;</p><table')
    manifest = _manifest(
        [
            _open("/uma"),
            {
                "kind": "extract_text",
                "id": "ler_chave",
                "title": "Ler a chave",
                "target": _target("chave", {"by": "css", "value": "#chave"}),
                "into": "chave",
            },
            {
                "kind": "click",
                "id": "cancelar",
                "title": "Cancelar a nota",
                "effect": "irreversible",
                "timeout_ms": 1_000,
                "target": _target(
                    "botão Cancelar da nota",
                    {"by": "role", "value": "button", "name": "Cancelar"},
                    within={"by": "role", "value": "row"},
                    within_has_text="{{ vars.chave }}",
                ),
            },
        ]
    )
    assert policy.check(manifest) == []
    page = _Page()
    outcome = await run_robot(manifest, prepare_context=_serve({"/uma": pagina}), after_run=page)
    result = outcome.result
    assert result.state is RunState.FAILED, result.error_message
    assert result.error_code == ErrorCode.TEMPLATE_INVALIDO
    assert result.failed_step_id == "cancelar"
    assert not result.irreversible_started
    assert page.title == "Notas"


# -- seletores CSS/XPath e endereços com valores ------------------------------------------

_BOTOES = """<!doctype html><meta charset="utf-8"><title>Notas</title>
<button data-nota="7" onclick="document.title='cancelou 7'">Cancelar 7</button>
<button id="excluir-tudo" onclick="document.title='EXCLUIU TUDO'">Excluir todas</button>"""


@pytest.mark.parametrize(
    ("by", "value", "payload"),
    [
        ("css", 'button[data-nota="{{ inputs.numero }}"]', 'x"], #excluir-tudo, [data-nota="y'),
        (
            "xpath",
            "//button[@data-nota='{{ inputs.numero }}']",
            "x' or @id='excluir-tudo",
        ),
    ],
)
async def test_dynamic_raw_selector_is_refused(
    run_robot: RobotRunner, by: str, value: str, payload: str
) -> None:
    manifest = _manifest(
        [
            _open("/notas"),
            {
                "kind": "click",
                "id": "cancelar",
                "title": "Cancelar a nota",
                "effect": "irreversible",
                "target": _target("botão Cancelar", {"by": by, "value": value}),
            },
        ],
        inputs=[{"name": "numero", "title": "Número", "type": "string"}],
    )
    assert "seletor_dinamico" in [v.code for v in policy.check(manifest)]
    page = _Page()
    outcome = await run_robot(
        manifest,
        {"numero": payload},
        prepare_context=_serve({"/notas": _BOTOES}),
        after_run=page,
    )
    assert outcome.result.state is RunState.FAILED
    assert outcome.result.error_code == ErrorCode.TEMPLATE_INVALIDO
    assert outcome.result.failed_step_id == "cancelar"
    assert not outcome.result.irreversible_started
    assert page.title == "Notas"


def _ver_nota() -> Manifest:
    return _manifest(
        [
            {
                "kind": "navigate",
                "id": "abrir_nota",
                "title": "Abrir a nota",
                "url": f"{BASE}/notas/{{{{ inputs.numero }}}}/ver?q={{{{ inputs.busca }}}}",
            }
        ],
        inputs=[
            {"name": "numero", "title": "Número", "type": "string"},
            {"name": "busca", "title": "Busca", "type": "string", "required": False},
        ],
    )


async def test_navigate_encodes_each_value(run_robot: RobotRunner) -> None:
    requests: list[str] = []
    for inputs in (
        {"numero": "123?confirmar=1#", "busca": "a b&c=d/e"},
        {"numero": "7"},  # na consulta, um valor vazio não muda nada
    ):
        outcome = await run_robot(
            _ver_nota(),
            inputs,
            mode=RunMode.UNTIL_FIRST_WRITE,
            prepare_context=_serve({}, requests=requests),
        )
        assert outcome.result.state is RunState.SUCCEEDED, outcome.result.error_message
    assert requests == [
        f"{BASE}/notas/123%3Fconfirmar%3D1%23/ver?q=a%20b%26c%3Dd%2Fe",
        f"{BASE}/notas/7/ver?q=",
    ]


@pytest.mark.parametrize(
    "numero",
    [
        "123/cancelar",  # um servidor ASGI decodifica %2F e chegaria em /notas/123/cancelar
        "123\\cancelar",
        "",  # o trecho sumiria: /notas//ver
        "..",
        ".",
    ],
)
async def test_navigate_refuses_path_values_before_any_step(
    run_robot: RobotRunner, numero: str
) -> None:
    requests: list[str] = []
    outcome = await run_robot(
        _ver_nota(),
        {"numero": numero},
        mode=RunMode.UNTIL_FIRST_WRITE,
        prepare_context=_serve({}, requests=requests),
    )
    result = outcome.result
    assert result.state is RunState.FAILED
    assert result.error_code == ErrorCode.ENTRADA_INVALIDA
    assert "a entrada 'numero' não pode ir no caminho" in (result.error_message or "")
    assert outcome.kinds() == [RunEventKind.RUN_STARTED, RunEventKind.RUN_FAILED]
    assert requests == []


async def test_navigate_refuses_a_path_value_from_the_page_when_the_step_runs(
    run_robot: RobotRunner,
) -> None:
    manifest = _manifest(
        [
            _open("/inicio"),
            {
                "kind": "extract_text",
                "id": "ler_numero",
                "title": "Ler o número",
                "target": _target("número", {"by": "css", "value": "#numero"}),
                "into": "numero",
            },
            {
                "kind": "navigate",
                "id": "abrir_nota",
                "title": "Abrir a nota",
                "url": f"{BASE}/notas/{{{{ vars.numero }}}}",
            },
        ]
    )
    assert policy.check(manifest) == []
    requests: list[str] = []
    pagina = '<!doctype html><title>Início</title><p id="numero">123/cancelar</p>'
    outcome = await run_robot(
        manifest,
        mode=RunMode.UNTIL_FIRST_WRITE,
        prepare_context=_serve({"/inicio": pagina}, requests=requests),
    )
    result = outcome.result
    assert result.state is RunState.FAILED
    assert result.error_code == ErrorCode.TEMPLATE_INVALIDO
    assert result.failed_step_id == "abrir_nota"
    assert requests == [f"{BASE}/inicio"]


async def test_bad_item_path_value_fails_before_any_write(run_robot: RobotRunner) -> None:
    manifest = _manifest(
        [
            {
                "kind": "for_each",
                "id": "cada_nota",
                "title": "Para cada nota",
                "effect": "write",
                "items": "inputs.notas",
                "steps": [
                    {
                        "kind": "navigate",
                        "id": "abrir_nota",
                        "title": "Abrir a nota",
                        "url": f"{BASE}/notas/{{{{ item.numero }}}}",
                    },
                    {
                        "kind": "click",
                        "id": "gravar",
                        "title": "Gravar a nota",
                        "effect": "write",
                        "target": _target(
                            "botão Gravar", {"by": "role", "value": "button", "name": "Gravar"}
                        ),
                    },
                ],
            }
        ],
        inputs=[_list_input("notas", {"name": "numero", "title": "Número", "type": "string"})],
    )
    assert policy.check(manifest) == []
    pagina = (
        "<!doctype html><title>Nota</title><button onclick=\"fetch('/gravou')\">Gravar</button>"
    )
    requests: list[str] = []
    outcome = await run_robot(
        manifest,
        {"notas": [{"numero": "1"}, {"numero": "2"}, {"numero": ".."}]},
        prepare_context=_serve({"/notas/1": pagina, "/notas/2": pagina}, requests=requests),
    )
    result = outcome.result
    assert result.state is RunState.FAILED, result.error_message
    assert result.error_code == ErrorCode.ENTRADA_INVALIDA
    assert "o campo 'notas[2].numero' não pode ir no caminho" in (result.error_message or "")
    assert outcome.kinds() == [RunEventKind.RUN_STARTED, RunEventKind.RUN_FAILED]
    assert requests == []  # nenhuma nota foi aberta nem gravada


async def test_navigate_refuses_a_dynamic_port(run_robot: RobotRunner) -> None:
    manifest = _manifest(
        [
            {
                "kind": "navigate",
                "id": "abrir",
                "title": "Abrir",
                "url": f"{BASE}:{{{{ inputs.porta }}}}/",
            }
        ],
        inputs=[{"name": "porta", "title": "Porta", "type": "string"}],
    )
    assert "dominio_dinamico" in [v.code for v in policy.check(manifest)]
    requests: list[str] = []
    outcome = await run_robot(
        manifest, {"porta": "8443"}, prepare_context=_serve({}, requests=requests)
    )
    assert outcome.result.state is RunState.FAILED
    assert outcome.result.error_code == ErrorCode.DOMINIO_NAO_PERMITIDO
    assert requests == []


# -- a máscara nunca muda o que o plano de controle lê nos eventos -------------------------

_EMITIR = """<!doctype html><meta charset="utf-8"><title>Emissão</title>
<button onclick="document.title='EMITIU'">Emitir</button>"""

#: Pedaços de chaves e valores constantes dos eventos: "irreversivel", "irreversible",
#: "irreversivel_iniciado", "sem_confirmacao", "succeeded", "click" e o id "emitir".
_PEDACOS = ["vers", "ivel", "rever", "firma", "ceed", "clic", "miti"]


def _emitir() -> Manifest:
    return _manifest(
        [
            _open("/emitir"),
            {
                "kind": "click",
                "id": "emitir",
                "title": "Emitir a nota",
                "effect": "irreversible",
                "target": _target(
                    "botão Emitir", {"by": "role", "value": "button", "name": "Emitir"}
                ),
            },
        ]
    )


@pytest.mark.parametrize("redact_values", [["vers"], ["ivel"], ["rever"], _PEDACOS])
async def test_masking_never_hides_the_irreversible_warning(
    run_robot: RobotRunner, tmp_path: Path, redact_values: list[str]
) -> None:
    # Um segredo que é um pedaço de "irreversivel" não pode apagar o aviso: sem ele,
    # o executor clicaria sem esperar a confirmação do plano de controle.
    inner = ListEventSink()
    page = _Page()
    outcome = await run_robot(
        _emitir(),
        events=AckingEventSink(inner, tmp_path / "sem-confirmacao", timeout_s=0.3),
        prepare_context=_serve({"/emitir": _EMITIR}),
        after_run=page,
        redact_values=redact_values,
    )
    result = outcome.result
    assert result.state is RunState.FAILED, result.error_message
    assert result.error_code == ErrorCode.SEM_CONFIRMACAO
    assert not result.irreversible_started
    assert page.title == "Emissão"  # o botão não foi clicado
    [warning] = [event for event in inner.events if needs_ack(event)]
    assert warning.step_id == "emitir"
    assert set(warning.data) == {"tipo", "titulo", "efeito", "irreversivel"}
    assert warning.data["tipo"] == "click"
    assert warning.data["efeito"] == "irreversible"
    assert warning.data["irreversivel"] is True
    finished = inner.events[-1]
    assert finished.kind is RunEventKind.RUN_FAILED
    assert finished.data["codigo"] == "sem_confirmacao"
    assert finished.data["estado"] == "failed"
    assert finished.data["passo"] == "emitir"
    assert finished.data["irreversivel_iniciado"] is False
    assert_seq(inner.events)


async def test_masking_keeps_event_keys_and_executor_constants(
    run_robot: RobotRunner, tmp_path: Path
) -> None:
    acks = tmp_path / "confirmacoes"
    acks.mkdir()
    for seq in range(20):
        ack_path(acks, seq).touch()
    inner = ListEventSink()
    page = _Page()
    outcome = await run_robot(
        _emitir(),
        events=AckingEventSink(inner, acks, timeout_s=5),
        prepare_context=_serve({"/emitir": _EMITIR}),
        after_run=page,
        redact_values=_PEDACOS,
    )
    assert outcome.result.state is RunState.SUCCEEDED, outcome.result.error_message
    assert page.title == "EMITIU"
    [warning] = [event for event in inner.events if needs_ack(event)]
    assert warning.data["efeito"] == "irreversible"
    finished = inner.events[-1]
    assert finished.kind is RunEventKind.RUN_SUCCEEDED
    assert finished.data["estado"] == "succeeded"
    assert finished.data["irreversivel_iniciado"] is True
    assert finished.data["passo_irreversivel"] == "emitir"
    # Os textos livres continuam mascarados: o título do passo traz "miti".
    started = [e for e in inner.events if e.kind is RunEventKind.STEP_STARTED]
    assert started[-1].data["titulo"] == f"E{MASK}r a nota"


# -- máscara de segredos nas saídas e nos artefatos ---------------------------------------

_CPF = "52998224725"  # login do portal: um CPF só com dígitos
_SENHA = "s3nh4-forte"
_RECIBO = b"recibo de teste\n"
_PAINEL = f"""<!doctype html><meta charset="utf-8"><title>Painel</title>
<p id="contrib">{_CPF}</p>
<p id="formatado">52.998.224.725</p>
<p id="quantidade">1.234</p>
<table aria-label="Resumo">
  <thead><tr><th>Notas de {_CPF}</th><th>Total {_CPF}</th><th>Total {_SENHA}</th><th>sha256</th></tr></thead>
  <tbody><tr><td>1</td><td>2</td><td>3</td><td>x-{_CPF}</td></tr></tbody>
</table>
<a href="/recibo">Baixar recibo</a>"""


def _ler_painel() -> Manifest:
    def text(step_id: str, element: str) -> dict[str, Any]:
        return {
            "kind": "extract_text",
            "id": f"ler_{step_id}",
            "title": f"Ler {step_id}",
            "target": _target(step_id, {"by": "css", "value": f"#{element}"}),
            "into": step_id,
        }

    def output(name: str, kind: str) -> dict[str, Any]:
        return {"name": name, "title": name, "type": kind, "source": f"vars.{name}"}

    return _manifest(
        [
            _open("/painel"),
            text("contrib", "contrib"),
            text("formatado", "formatado"),
            text("quantidade", "quantidade"),
            {
                "kind": "extract_table",
                "id": "ler_resumo",
                "title": "Ler o resumo",
                "target": _target("resumo", {"by": "role", "value": "table", "name": "Resumo"}),
                "into": "resumo",
            },
            {
                "kind": "download",
                "id": "baixar",
                "title": "Baixar o recibo",
                "target": _target(
                    "link do recibo", {"by": "role", "value": "link", "name": "Baixar recibo"}
                ),
                "save_as": "recibo-{{ vars.contrib }}.txt",
                "into": "recibo",
            },
        ],
        outputs=[
            output("contrib", "integer"),
            output("formatado", "integer"),
            output("quantidade", "integer"),
            output("resumo", "list"),
            output("recibo", "file"),
        ],
    )


async def test_outputs_keys_and_artifact_names_are_masked(run_robot: RobotRunner) -> None:
    digest = hashlib.sha256(_RECIBO).hexdigest()
    hex_value = digest[10:20]  # um "segredo" que por acaso aparece no hash
    manifest = _ler_painel()
    assert policy.check(manifest) == []
    outcome = await run_robot(
        manifest,
        prepare_context=_serve({"/painel": _PAINEL}, downloads={"/recibo": _RECIBO}),
        redact_values=[_CPF, _SENHA, hex_value],
    )
    result = outcome.result
    assert result.state is RunState.SUCCEEDED, result.error_message
    assert result.outputs is not None
    # Mascarado antes de virar número; nem o número convertido pode carregar o segredo.
    assert result.outputs["contrib"] == MASK
    assert result.outputs["formatado"] == MASK
    assert result.outputs["quantidade"] == 1234
    # Cabeçalhos da página viram chaves: também são mascarados (o repetido vira
    # coluna_N, como qualquer cabeçalho repetido), e uma coluna chamada "sha256" não
    # escapa da máscara.
    assert result.outputs["resumo"] == [
        {"Notas de ***": "1", "Total ***": "2", "coluna_3": "3", "sha256": "x-***"}
    ]
    recibo = result.outputs["recibo"]
    [ref] = [a for a in result.artifacts if a.kind == ArtifactKind.DOWNLOAD]
    assert ref.name == "recibo-_.txt"
    assert recibo["name"] == ref.name
    assert recibo["sha256"] == ref.sha256 == digest  # o hash do próprio executor fica intacto
    assert outcome.of(RunEventKind.ARTIFACT, "baixar")[0].data["sha256"] == digest
    started = outcome.of(RunEventKind.RUN_STARTED)[0]
    assert started.data["fingerprint"] == manifest.fingerprint()

    everything = outcome.dump() + "\n".join(a.name + a.path for a in result.artifacts)
    for secret in (_CPF, _SENHA):
        assert secret not in everything
    assert_seq(outcome.events)


_NUMEROS_GRANDES = """<!doctype html><meta charset="utf-8"><title>Números</title>
<p id="grande">12.345.678.901.234.567,00</p>
<p id="expoente">9,876543210123456E16</p>
<p id="zeros">0.012.345</p>
<p id="normal">1.234,50</p>"""


async def test_numeric_outputs_never_show_a_secret(run_robot: RobotRunner) -> None:
    def text(name: str) -> dict[str, Any]:
        return {
            "kind": "extract_text",
            "id": f"ler_{name}",
            "title": f"Ler {name}",
            "target": _target(name, {"by": "css", "value": f"#{name}"}),
            "into": name,
        }

    def output(name: str, kind: str) -> dict[str, Any]:
        return {"name": name, "title": name, "type": kind, "source": f"vars.{name}"}

    manifest = _manifest(
        [_open("/numeros"), text("grande"), text("expoente"), text("zeros"), text("normal")],
        outputs=[
            output("grande", "number"),
            output("expoente", "number"),
            output("zeros", "integer"),
            output("normal", "number"),
        ],
    )
    outcome = await run_robot(
        manifest,
        prepare_context=_serve({"/numeros": _NUMEROS_GRANDES}),
        # 17 dígitos: o float arredonda o último e str() usa expoente; um segredo
        # com zeros à esquerda some no inteiro.
        redact_values=["12345678901234567", "98765432101234560", "0012345"],
    )
    assert outcome.result.state is RunState.SUCCEEDED, outcome.result.error_message
    assert outcome.result.outputs == {
        "grande": MASK,
        "expoente": MASK,
        "zeros": MASK,
        "normal": 1234.5,
    }


# -- campos opcionais de item, limite de itens e números -----------------------------------

_FORMULARIO = """<!doctype html><meta charset="utf-8"><title>Formulário</title>
<label>Número <input name="numero"></label>
<label>Observação <input name="obs"></label>
<button onclick="document.querySelector('#log').textContent +=
  document.querySelector('[name=numero]').value + ':' +
  document.querySelector('[name=obs]').value + '|'">Gravar</button>
<p id="log"></p>"""


def _fill(step_id: str, label: str, template: str, effect: str = "write") -> dict[str, Any]:
    return {
        "kind": "fill",
        "id": step_id,
        "title": f"Digitar {label}",
        "effect": effect,
        "target": _target(f"campo {label}", {"by": "label", "value": label}),
        "value": {"source": "text", "template": template},
    }


def _gravar_linhas(max_items: int = 1000) -> Manifest:
    return _manifest(
        [
            _open("/form"),
            {
                "kind": "for_each",
                "id": "cada_linha",
                "title": "Para cada linha",
                "effect": "write",
                "items": "inputs.linhas",
                "max_items": max_items,
                "steps": [
                    _fill("digitar_numero", "Número", "{{ item.numero }}"),
                    _fill("digitar_obs", "Observação", "{{ item.obs }}"),
                    {
                        "kind": "click",
                        "id": "gravar",
                        "title": "Gravar a linha",
                        "effect": "write",
                        "target": _target(
                            "botão Gravar", {"by": "role", "value": "button", "name": "Gravar"}
                        ),
                    },
                ],
            },
        ],
        inputs=[
            _list_input(
                "linhas",
                {"name": "numero", "title": "Número", "type": "string"},
                {"name": "obs", "title": "Observação", "type": "string", "required": False},
            )
        ],
    )


async def test_missing_optional_item_field_is_typed_empty(run_robot: RobotRunner) -> None:
    manifest = _gravar_linhas()
    assert policy.check(manifest) == []
    page = _Page()
    linhas = [{"numero": "1", "obs": "a"}, {"numero": "2"}, {"numero": "3", "obs": None}]
    outcome = await run_robot(
        manifest,
        {"linhas": linhas},
        prepare_context=_serve({"/form": _FORMULARIO}),
        after_run=page,
    )
    assert outcome.result.state is RunState.SUCCEEDED, outcome.result.error_message
    assert page.log == "1:a|2:|3:|"
    assert linhas[1] == {"numero": "2"}  # as entradas de quem chamou não mudam


async def test_missing_required_item_field_fails_before_any_step(
    run_robot: RobotRunner,
) -> None:
    outcome = await run_robot(
        _gravar_linhas(),
        {"linhas": [{"numero": "1", "obs": "a"}, {"obs": "b"}, {"numero": None}]},
        prepare_context=_serve({"/form": _FORMULARIO}),
    )
    result = outcome.result
    assert result.state is RunState.FAILED
    assert result.error_code == ErrorCode.ENTRADA_INVALIDA
    assert "linhas[1].numero" in (result.error_message or "")
    assert "linhas[2].numero" in (result.error_message or "")
    assert outcome.kinds() == [RunEventKind.RUN_STARTED, RunEventKind.RUN_FAILED]


async def test_list_over_loop_limit_fails_before_any_write(run_robot: RobotRunner) -> None:
    manifest = _manifest(
        [
            _open("/form"),
            _fill("abrir_lote", "Número", "lote", effect="write"),
            {
                "kind": "for_each",
                "id": "cada",
                "title": "Para cada item",
                "items": "inputs.itens",
                "max_items": 2,
                "steps": [_fill("digitar_item", "Observação", "{{ item.x }}")],
            },
        ],
        inputs=[_list_input("itens", {"name": "x", "title": "X", "type": "string"})],
    )
    assert manifest.input_json_schema()["properties"]["itens"]["maxItems"] == 2
    outcome = await run_robot(
        manifest,
        {"itens": [{"x": "1"}, {"x": "2"}, {"x": "3"}]},
        prepare_context=_serve({"/form": _FORMULARIO}),
    )
    result = outcome.result
    assert result.state is RunState.FAILED
    assert result.error_code == ErrorCode.LIMITE_DE_ITENS_EXCEDIDO
    assert result.failed_step_id == "cada"
    assert result.steps_done == 0
    assert outcome.kinds() == [RunEventKind.RUN_STARTED, RunEventKind.RUN_FAILED]


_NUMEROS = """<!doctype html><meta charset="utf-8"><title>Números</title>
<label>Quantidade <input name="qtd"></label>
<label>Taxa <input name="taxa"></label>
<label>Pequena <input name="pequena"></label>
<label>Item <input name="item"></label>"""


def _digitar_numeros() -> Manifest:
    return _manifest(
        [
            _open("/numeros"),
            _fill("digitar_qtd", "Quantidade", "{{ inputs.qtd }}", effect="read"),
            _fill("digitar_taxa", "Taxa", "{{ inputs.taxa }}", effect="read"),
            _fill("digitar_pequena", "Pequena", "{{ inputs.pequena }}", effect="read"),
            {
                "kind": "for_each",
                "id": "cada",
                "title": "Para cada item",
                "items": "inputs.itens",
                "steps": [_fill("digitar_item", "Item", "{{ item.n }}", effect="read")],
            },
        ],
        inputs=[
            {"name": "qtd", "title": "Quantidade", "type": "integer"},
            {"name": "taxa", "title": "Taxa", "type": "number"},
            {"name": "pequena", "title": "Pequena", "type": "number"},
            _list_input("itens", {"name": "n", "title": "N", "type": "integer"}),
        ],
    )


async def test_whole_numbers_are_typed_without_decimals(run_robot: RobotRunner) -> None:
    page = _Page()
    outcome = await run_robot(
        _digitar_numeros(),
        {"qtd": 5.0, "taxa": 12345678901234567.0, "pequena": 0.00001, "itens": [{"n": 2.0}]},
        prepare_context=_serve({"/numeros": _NUMEROS}),
        after_run=page,
    )
    assert outcome.result.state is RunState.SUCCEEDED, outcome.result.error_message
    assert page.values == {
        "qtd": "5",
        "taxa": "12345678901234568",
        "pequena": "0.00001",
        "item": "2",
    }


@pytest.mark.parametrize(
    ("inputs", "problem"),
    [
        ({"qtd": 5.5}, "qtd"),
        ({"qtd": 1e20}, "qtd"),
        ({"taxa": 1e20}, "taxa"),
        ({"taxa": float("nan")}, "taxa"),
        ({"itens": [{"n": 2.5}]}, "itens[0].n"),
    ],
)
async def test_numbers_that_cannot_be_typed_fail_before_any_step(
    run_robot: RobotRunner, inputs: dict[str, Any], problem: str
) -> None:
    values = {"qtd": 1, "taxa": 1.5, "pequena": 2, "itens": [], **inputs}
    outcome = await run_robot(
        _digitar_numeros(), values, prepare_context=_serve({"/numeros": _NUMEROS})
    )
    assert outcome.result.state is RunState.FAILED
    assert outcome.result.error_code == ErrorCode.ENTRADA_INVALIDA
    assert f"'{problem}'" in (outcome.result.error_message or "")
    assert outcome.kinds() == [RunEventKind.RUN_STARTED, RunEventKind.RUN_FAILED]

"""Onde a ação de um passo que grava cai e o que os campos digitados ainda têm antes dela.

Páginas de um servidor HTTP de verdade, num domínio de teste com porta; o registro do que
gravou é o do servidor (o corpo de cada ``POST /api``).

- Um elemento de outro documento por cima do alvo (um iframe de atendimento sobre o botão,
  a página principal sobre um iframe) nunca recebe a ação: o Playwright espera o alvo ficar
  livre (o clique vai sem ``force``), e um clique cujos eventos não chegam à janela do alvo
  falha sem agir. Uma camada de carregamento ou um diálogo modal só atrasa o clique.
- A linha escolhida pelo texto ainda precisa ter, na hora do evento, um elemento cujo texto
  inteiro é a chave: a linha reaproveitada de "12" para "120" é recusada.
- O que a página muda num campo digitado durante os passos do robô que agem (a sugestão do
  autocompletar, a formatação quando o foco sai, a busca limpa depois do Enter) vale; o que
  ela muda sozinha depois do último passo que agiu (durante uma espera, depois de um
  ``replaceState``) é recusado antes de gravar.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import BrowserContext
from playwright.async_api import Error as PlaywrightError

from rapa.core import policy
from rapa.core.contracts import RunState
from rapa.core.robot import Manifest
from rapa.runner.errors import ErrorCode
from rapa.runner.interpreter import _typed_value_matches
from tests.runner.support import Outcome, RobotRunner

pytestmark = pytest.mark.browser

HOST = "paginas.rapa.test"

type App = Callable[[str, str, dict[str, list[str]]], tuple[int, dict[str, str], str]]


class _Server:
    """Servidor HTTP de verdade, com o registro, do lado dele, do que cada ``POST /api`` gravou."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.app: App = lambda method, path, form: (404, {}, "")
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _serve(self, method: str) -> None:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length).decode() if length else ""
                form = parse_qs(raw, keep_blank_values=True)
                form["_raw"] = [raw]
                status, headers, body = server.app(method, self.path, form)
                data = body.encode()
                self.send_response(status)
                self.send_header("content-type", "text/html; charset=utf-8")
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._serve("GET")

            def do_POST(self) -> None:
                self._serve("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.base = f"http://{HOST}:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def srv() -> Iterator[_Server]:
    server = _Server()
    yield server
    server.close()


def _manifest(steps: list[dict[str, Any]], **extra: Any) -> Manifest:
    return Manifest.model_validate(
        {
            "name": "robo_de_teste",
            "title": "Robô de teste",
            "description": "Robô montado pela verificação.",
            "allowed_domains": [HOST],
            "steps": steps,
            **extra,
        }
    )


def _nav(base: str, path: str, step_id: str = "abrir") -> dict[str, Any]:
    return {"kind": "navigate", "id": step_id, "title": "Abrir a página", "url": f"{base}{path}"}


def _target(intent: str, locator: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"intent": intent, "locators": [locator], **extra}


def _row(key: str) -> dict[str, Any]:
    return {"within": {"by": "role", "value": "row"}, "within_has_text": key}


def _click(
    step_id: str,
    name: str,
    *,
    key: str | None = None,
    effect: str = "irreversible",
    nav: bool = False,
    role: str = "button",
    by: str = "role",
    timeout: int = 3_000,
) -> dict[str, Any]:
    locator = (
        {"by": "role", "value": role, "name": name, "exact": True}
        if by == "role"
        else {"by": "text", "value": name, "exact": True}
    )
    step: dict[str, Any] = {
        "kind": "click",
        "id": step_id,
        "title": f"Clicar em {name}",
        "effect": effect,
        "timeout_ms": timeout,
        "target": _target(f"botão {name}", locator, **(_row(key) if key else {})),
    }
    if nav:
        step["expect_navigation"] = True
    return step


def _page(body: str, head: str = "") -> str:
    return f"<!doctype html><html><head><meta charset='utf-8'><title>Notas</title>{head}</head><body>{body}</body></html>"


def _table(rows: str) -> str:
    return (
        "<table aria-label='Notas'><thead><tr><th>numero</th><th>acao</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


async def _run(run_robot: RobotRunner, manifest: Manifest, **kw: Any) -> Outcome:
    assert policy.check(manifest) == []

    async def settle(context: BrowserContext) -> None:
        if context.pages:
            try:
                await context.pages[0].wait_for_timeout(700)
            except PlaywrightError:
                pass

    return await run_robot(manifest, after_run=settle, **kw)


def _fill(
    step_id: str, label: str, text: str, effect: str = "read", sequential: bool = False
) -> dict[str, Any]:
    return {
        "kind": "fill",
        "id": step_id,
        "title": f"Digitar {label}",
        "effect": effect,
        "sequential": sequential,
        "target": _target(f"campo {label}", {"by": "label", "value": label, "exact": True}),
        "value": {"source": "text", "template": text},
    }


def _save(effect: str = "write") -> dict[str, Any]:
    return _click("salvar", "Salvar", effect=effect)


def _api_app(srv: _Server, pages: dict[str, str]) -> App:
    def app(method: str, path: str, form: dict[str, list[str]]) -> tuple[int, dict[str, str], str]:
        p = urlsplit(path).path
        if p in pages:
            return 200, {}, pages[p]
        if p == "/api" and method == "POST":
            srv.log.append(form["_raw"][0])
            return 200, {}, "ok"
        return 404, {}, "nada"

    return app


def _post(expr: str) -> str:
    return f"fetch('/api',{{method:'POST',body:{expr}}})"


_OVERLAY = _page(
    "<div id='cobre' style='position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:10'>Carregando</div>"
    f"<button type='button' onclick=\"{_post(chr(39) + 'S;' + chr(39))}\">Salvar</button>"
    "<script>setTimeout(()=>document.getElementById('cobre').remove(),800)</script>"
)


_MODAL = _page(
    f"<button type='button' onclick=\"{_post(chr(39) + 'S;' + chr(39))}\">Salvar</button>"
    "<dialog id='d'><p>Aguarde</p></dialog>"
    "<script>const d=document.getElementById('d');d.showModal();setTimeout(()=>d.close(),800)</script>"
)


def _hover_swap(first: str, second: str) -> str:
    btn = (
        "<button type='button' onclick=\"fetch('/api',{method:'POST',"
        "body:'C'+this.closest('tr').cells[0].textContent+';'})\">Cancelar</button>"
    )
    return _page(
        _table(f"<tr><td>{first}</td><td>{btn}</td></tr><tr><td>{second}</td><td>{btn}</td></tr>")
        + "<script>let feito=false;document.querySelector('tbody').addEventListener('mouseover',()=>{"
        "if(feito)return;feito=true;const c=[...document.querySelectorAll('tbody tr')].map(r=>r.cells[0]);"
        "[c[0].textContent,c[1].textContent]=[c[1].textContent,c[0].textContent]})</script>"
    )


_TYPEAHEAD = _page(
    "<label for='cid'>Cidade</label><input id='cid' autocomplete='off'>"
    "<ul id='sug' role='listbox' aria-label='Sugestões'></ul>"
    f"<button type='button' onclick=\"{_post('document.getElementById(' + chr(39) + 'cid' + chr(39) + ').value+' + chr(39) + ';' + chr(39))}\">Salvar</button>"
    "<script>const cid=document.getElementById('cid'),sug=document.getElementById('sug');"
    "cid.addEventListener('input',()=>{sug.innerHTML='';if(cid.value.startsWith('S')){"
    "const li=document.createElement('li');li.setAttribute('role','option');li.textContent='São Paulo - SP';"
    "li.onclick=()=>{cid.value='São Paulo - SP';sug.innerHTML=''};sug.appendChild(li)}})</script>"
)


_BLUR_DECIMAL = _page(
    "<label for='qtd'>Quantidade</label><input id='qtd'>"
    "<label for='desc'>Descrição</label><input id='desc'>"
    f"<button type='button' onclick=\"{_post('document.getElementById(' + chr(39) + 'qtd' + chr(39) + ').value+' + chr(39) + ';' + chr(39))}\">Salvar</button>"
    "<script>const q=document.getElementById('qtd');q.addEventListener('blur',()=>{"
    "const n=Number(q.value.replace(',','.'));if(!isNaN(n)&&q.value!=='')q.value=n.toFixed(2).replace('.',',')})</script>"
)


_SEARCH = _page(
    "<label for='q'>Buscar</label><input id='q'>"
    "<table aria-label='Notas'><thead><tr><th>numero</th><th>acao</th></tr></thead><tbody id='res'></tbody></table>"
    "<script>const q=document.getElementById('q');q.addEventListener('keydown',(e)=>{if(e.key!=='Enter')return;"
    "const v=q.value;q.value='';document.getElementById('res').innerHTML='<tr><td>'+v+'</td><td><button type=\\'button\\' "
    "onclick=\\'fetch(&quot;/api&quot;,{method:&quot;POST&quot;,body:&quot;E&quot;+this.closest(&quot;tr&quot;).cells[0].textContent+&quot;;&quot;})\\'>Excluir</button></td></tr>'})</script>"
)


_CENTS = _page(
    "<label for='v'>Valor</label><input id='v'>"
    f"<button type='button' onclick=\"{_post('document.getElementById(' + chr(39) + 'v' + chr(39) + ').value+' + chr(39) + ';' + chr(39))}\">Salvar</button>"
    "<script>const v=document.getElementById('v');v.addEventListener('input',()=>{"
    "const d=v.value.replace(/\\D/g,'').replace(/^0+/,'').padStart(3,'0');"
    "v.value=d.slice(0,-2)+','+d.slice(-2)})</script>"
)


def _cep_iframe_app(srv: _Server, delay_ms: int) -> App:
    inner = _cep_page(delay_ms)

    def app(method: str, path: str, form: dict[str, list[str]]) -> tuple[int, dict[str, str], str]:
        p = urlsplit(path).path
        if p == "/host":
            return (
                200,
                {},
                _page("<iframe id='f' src='/form' style='width:600px;height:400px'></iframe>"),
            )
        if p == "/form":
            return 200, {}, inner
        if p == "/api" and method == "POST":
            srv.log.append(form["_raw"][0])
            return 200, {}, "ok"
        return 404, {}, "nada"

    return app


def _cep_page(delay_ms: int) -> str:
    return _page(
        "<label for='cep'>CEP</label><input id='cep'>"
        "<label for='log'>Logradouro</label><input id='log'>"
        "<label for='num'>Número</label><input id='num'>"
        "<p id='ok' hidden>Endereço conferido</p>"
        "<button type='button' onclick=\"fetch('/api',{method:'POST',body:document.getElementById('log').value+';'})\">Salvar</button>"
        "<script>document.getElementById('cep').addEventListener('change',()=>{"
        f"setTimeout(()=>{{document.getElementById('log').value='Rua do CEP'}},{delay_ms});"
        "setTimeout(()=>{document.getElementById('ok').hidden=false},600)})</script>"
    )


_CEP_REPLACE = _page(
    "<label for='cep'>CEP</label><input id='cep'>"
    "<label for='log'>Logradouro</label><input id='log'>"
    "<label for='num'>Número</label><input id='num'>"
    "<p id='ok' hidden>Endereço conferido</p>"
    "<button type='button' onclick=\"fetch('/api',{method:'POST',body:document.getElementById('log').value+';'})\">Salvar</button>"
    "<script>document.getElementById('num').addEventListener('change',()=>{history.replaceState({},'','/p?passo=2');"
    "setTimeout(()=>{document.getElementById('log').value='Rua do CEP'},150);"
    "setTimeout(()=>{document.getElementById('ok').hidden=false},600)})</script>"
)


def _parent_cover_app(srv: _Server, handler: bool, effect_ms: int = 1500) -> App:
    onclick = " onclick=\"fetch('/api',{method:'POST',body:'CAPA;'})\"" if handler else ""
    host = _page(
        f"<div id='capa'{onclick} style='position:fixed;inset:0;z-index:10;background:rgba(0,0,0,.2)'></div>"
        "<iframe id='app' src='/form' style='width:600px;height:300px'></iframe>"
        f"<script>setTimeout(()=>document.getElementById('capa').remove(),{effect_ms})</script>"
    )
    form = _page(
        f"<button type='button' onclick=\"{_post(chr(39) + 'S;' + chr(39))}\">Salvar</button>"
    )
    return _api_app(srv, {"/host": host, "/form": form})


def _iframe_cover_app(srv: _Server, handler: bool) -> App:
    widget = _page(
        "<div style='position:fixed;inset:0;background:#eef'"
        + (" onclick=\"fetch('/api',{method:'POST',body:'WIDGET;'})\"" if handler else "")
        + ">Fale conosco</div>"
    )
    host = _page(
        f"<button type='button' style='margin:40px' onclick=\"{_post(chr(39) + 'S;' + chr(39))}\">Salvar</button>"
        "<iframe id='w' src='/widget' style='position:fixed;left:0;top:0;width:400px;height:200px;border:0;z-index:10'></iframe>"
        "<script>setTimeout(()=>document.getElementById('w').remove(),1500)</script>"
    )
    return _api_app(srv, {"/host": host, "/widget": widget})


def _iframe_cover_check_app(srv: _Server) -> App:
    widget = _page(
        "<div style='position:fixed;inset:0;background:#eef' onclick=\"fetch('/api',{method:'POST',body:'WIDGET;'})\">Fale conosco</div>"
    )
    host = _page(
        "<label style='display:block;margin:40px'><input type='checkbox' onchange=\"fetch('/api',{method:'POST',body:'K'+this.checked+';'})\"> Aceito</label>"
        "<iframe id='w' src='/widget' style='position:fixed;left:0;top:0;width:400px;height:200px;border:0;z-index:10'></iframe>"
        "<script>setTimeout(()=>document.getElementById('w').remove(),1500)</script>"
    )
    return _api_app(srv, {"/host": host, "/widget": widget})


def _wrote(outcome: Outcome, srv: _Server, state: RunState, log: str) -> None:
    assert "".join(srv.log) == log, outcome.result.error_message
    assert outcome.result.state is state, outcome.result.error_message


# -- um elemento de outro documento por cima do alvo ------------------------------------------


@pytest.mark.parametrize("effect", ["write", "irreversible"])
async def test_an_iframe_over_the_button_never_gets_the_click(
    run_robot: RobotRunner, srv: _Server, effect: str
) -> None:
    # Um widget num iframe cobre o Salvar por 1,5 s e grava quando clicado: o clique espera.
    srv.app = _iframe_cover_app(srv, handler=True)
    manifest = _manifest(
        [_nav(srv.base, "/host"), _click("salvar", "Salvar", effect=effect, timeout=5_000)]
    )
    outcome = await _run(run_robot, manifest)
    _wrote(outcome, srv, RunState.SUCCEEDED, "S;")


@pytest.mark.parametrize("effect", ["write", "irreversible"])
async def test_the_page_over_an_iframe_button_never_gets_the_click(
    run_robot: RobotRunner, srv: _Server, effect: str
) -> None:
    # O alvo está num iframe, e a página principal o cobre por 1,5 s com algo que grava.
    srv.app = _parent_cover_app(srv, handler=True)
    click = _click("salvar", "Salvar", effect=effect, timeout=5_000)
    click["target"]["frames"] = ["#app"]
    outcome = await _run(run_robot, _manifest([_nav(srv.base, "/host"), click]))
    _wrote(outcome, srv, RunState.SUCCEEDED, "S;")


async def test_a_check_under_an_iframe_never_goes_to_the_iframe(
    run_robot: RobotRunner, srv: _Server
) -> None:
    srv.app = _iframe_cover_check_app(srv)
    step = {
        "kind": "check",
        "id": "aceitar",
        "title": "Marcar Aceito",
        "effect": "write",
        "timeout_ms": 5_000,
        "checked": True,
        "target": _target("caixa Aceito", {"by": "label", "value": "Aceito", "exact": True}),
    }
    outcome = await _run(run_robot, _manifest([_nav(srv.base, "/host"), step]))
    _wrote(outcome, srv, RunState.SUCCEEDED, "Ktrue;")


@pytest.mark.parametrize("page", ["camada", "modal"])
@pytest.mark.parametrize("effect", ["write", "irreversible"])
async def test_a_loading_layer_or_a_modal_only_delays_the_click(
    run_robot: RobotRunner, srv: _Server, page: str, effect: str
) -> None:
    srv.app = _api_app(srv, {"/p": _OVERLAY if page == "camada" else _MODAL})
    manifest = _manifest(
        [_nav(srv.base, "/p"), _click("salvar", "Salvar", effect=effect, timeout=5_000)]
    )
    outcome = await _run(run_robot, manifest)
    _wrote(outcome, srv, RunState.SUCCEEDED, "S;")


# -- a linha na hora do evento --------------------------------------------------------------


@pytest.mark.parametrize(("first", "second"), [("12", "120"), ("120", "7")])
@pytest.mark.parametrize("effect", ["write", "irreversible"])
async def test_a_row_reused_when_the_pointer_arrives_is_refused(
    run_robot: RobotRunner, srv: _Server, first: str, second: str, effect: str
) -> None:
    # As duas linhas trocam de texto quando o mouse chega à tabela: a linha que foi
    # conferida passa a mostrar outra nota (também uma que contém a chave: "12" -> "120").
    srv.app = _api_app(srv, {"/notas": _hover_swap(first, second)})
    manifest = _manifest(
        [_nav(srv.base, "/notas"), _click("cancelar", "Cancelar", key=first, effect=effect)]
    )
    outcome = await _run(run_robot, manifest)
    state = RunState.FAILED if effect == "write" else RunState.INDETERMINATE
    _wrote(outcome, srv, state, "")
    assert outcome.result.error_code == ErrorCode.ELEMENTO_AMBIGUO


# -- os campos digitados antes de gravar ----------------------------------------------------


@pytest.mark.parametrize("effect", ["read", "write"])
async def test_the_suggestion_picked_in_a_typeahead_is_saved(
    run_robot: RobotRunner, srv: _Server, effect: str
) -> None:
    srv.app = _api_app(srv, {"/p": _TYPEAHEAD})
    manifest = _manifest(
        [
            _nav(srv.base, "/p"),
            _fill("digitar_cid", "Cidade", "São", effect=effect),
            _click("escolher", "São Paulo - SP", role="option", effect="read"),
            _save(),
        ]
    )
    outcome = await _run(run_robot, manifest)
    _wrote(outcome, srv, RunState.SUCCEEDED, "São Paulo - SP;")


@pytest.mark.parametrize("effect", ["read", "write"])
async def test_a_format_the_page_puts_when_the_field_loses_focus_is_saved(
    run_robot: RobotRunner, srv: _Server, effect: str
) -> None:
    srv.app = _api_app(srv, {"/p": _BLUR_DECIMAL})
    manifest = _manifest(
        [
            _nav(srv.base, "/p"),
            _fill("digitar_qtd", "Quantidade", "10", effect=effect),
            _fill("digitar_desc", "Descrição", "Parafuso", effect=effect),
            _save(),
        ]
    )
    outcome = await _run(run_robot, manifest)
    _wrote(outcome, srv, RunState.SUCCEEDED, "10,00;")


async def test_a_search_box_the_page_clears_after_enter_does_not_stop_the_write(
    run_robot: RobotRunner, srv: _Server
) -> None:
    srv.app = _api_app(srv, {"/p": _SEARCH})
    manifest = _manifest(
        [
            _nav(srv.base, "/p"),
            _fill("digitar_busca", "Buscar", "120"),
            {
                "kind": "press",
                "id": "enter",
                "title": "Buscar",
                "effect": "read",
                "key": "Enter",
                "target": _target(
                    "campo Buscar", {"by": "label", "value": "Buscar", "exact": True}
                ),
            },
            _click("excluir", "Excluir", key="120", effect="write"),
        ]
    )
    outcome = await _run(run_robot, manifest)
    _wrote(outcome, srv, RunState.SUCCEEDED, "E120;")


async def test_a_field_the_page_fills_during_a_wait_is_never_saved(
    run_robot: RobotRunner, srv: _Server
) -> None:
    # O CEP preenche o logradouro 150 ms depois de mudar, enquanto o robô espera o aviso.
    srv.app = _cep_iframe_app(srv, 150)
    steps = [
        _fill("digitar_cep", "CEP", "01001000"),
        _fill("digitar_log", "Logradouro", "Praça da Sé"),
        _fill("digitar_num", "Número", "100"),
        {
            "kind": "wait_for",
            "id": "esperar",
            "title": "Esperar a conferência",
            "target": _target(
                "aviso", {"by": "text", "value": "Endereço conferido", "exact": True}
            ),
            "state": "visible",
            "timeout_ms": 5_000,
        },
        _save(),
    ]
    for step in steps:
        step["target"]["frames"] = ["#f"]
    outcome = await _run(run_robot, _manifest([_nav(srv.base, "/host"), *steps]))
    _wrote(outcome, srv, RunState.FAILED, "")
    assert outcome.result.error_code == ErrorCode.ACAO_FALHOU
    assert outcome.result.failed_step_id == "salvar"
    assert "Praça" not in outcome.dump()


async def test_a_field_the_page_fills_after_a_replace_state_is_never_saved(
    run_robot: RobotRunner, srv: _Server
) -> None:
    # Uma mudança de endereço no mesmo documento não tira os campos da conferência.
    srv.app = _api_app(srv, {"/p": _CEP_REPLACE})
    manifest = _manifest(
        [
            _nav(srv.base, "/p"),
            _fill("digitar_log", "Logradouro", "Praça da Sé"),
            _fill("digitar_num", "Número", "100"),
            _fill("digitar_cep", "CEP", "01001000"),
            {
                "kind": "wait_for",
                "id": "esperar",
                "title": "Esperar a conferência",
                "target": _target(
                    "aviso", {"by": "text", "value": "Endereço conferido", "exact": True}
                ),
                "state": "visible",
                "timeout_ms": 5_000,
            },
            _save(),
        ]
    )
    outcome = await _run(run_robot, manifest)
    _wrote(outcome, srv, RunState.FAILED, "")
    assert outcome.result.failed_step_id == "salvar"


@pytest.mark.parametrize(("text", "log"), [("10,5", ""), ("-100,00", ""), ("100,00", "100,00;")])
async def test_a_cents_mask_that_changes_the_number_stops_a_write(
    run_robot: RobotRunner, srv: _Server, text: str, log: str
) -> None:
    srv.app = _api_app(srv, {"/p": _CENTS})
    manifest = _manifest(
        [_nav(srv.base, "/p"), _fill("digitar", "Valor", text, effect="write"), _save()]
    )
    outcome = await _run(run_robot, manifest)
    _wrote(outcome, srv, RunState.SUCCEEDED if log else RunState.FAILED, log)


# -- a comparação do texto digitado com o valor do campo ------------------------------------


@pytest.mark.parametrize(
    ("text", "value", "number", "matches"),
    [
        ("Praça da Sé", "Praça da Sé", False, True),
        ("52998224725", "529.982.247-25", False, True),
        ("01001000", "01001-000", False, True),
        ("11912345678", "(11) 91234-5678", False, True),
        ("1234", "R$ 12,34", False, True),
        ("1234,50", "R$ 1.234,50", False, True),
        ("1234,5", "1.234,50", False, True),
        ("1.234", "1.234,00", False, True),
        ("100,00", "100,00", False, True),
        ("10,5", "1,05", False, False),
        ("-100,00", "100,00", False, False),
        ("-100,00", "-R$ 100,00", False, True),
        ("1.234,00", "123.400,00", False, False),
        ("1234.56", "1.234,56", False, True),
        ("dez", "", True, False),
        ("12", "", False, False),
        ("1.234,56", "1.23456", True, False),
        ("Rua A", "Rua B", False, False),
        ("sp", "SP", False, False),
        ("  Parafuso ", "Parafuso", False, True),
        ("10", "10,00", False, True),
        ("10", "10,50", False, False),
        ("10", "0,10", False, False),
    ],
)
def test_the_typed_text_and_the_field_value(
    text: str, value: str, number: bool, matches: bool
) -> None:
    assert _typed_value_matches(text, value, browser=None, number=number) is matches

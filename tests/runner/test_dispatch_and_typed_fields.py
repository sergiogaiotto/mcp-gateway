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
  ``replaceState``) é recusado antes de gravar. Com campos digitados noutro documento
  que o do alvo, a releitura vem depois de o alvo ficar livre, e um primeiro evento que
  demora (um documento de outra origem por cima) é recusado.
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
        ("10", "10,000", False, False),
        ("0012345", "12345", False, False),
        ("0800", "800", False, False),
        ("10", "10.00", False, True),
        ("1000", "1000.00", False, True),
        ("10", "10,00 €", False, True),
        ("10", "10,00%", False, True),
        ("10", "10.000", False, False),
        ("10", "1.000.00", False, False),
        ("10", "10,00 1", False, False),
        ("100,00", "-100,00", False, False),
        ("10", "-10", False, False),
        ("10", "R$ -10,00", False, False),
        ("10", "(10,00)", False, False),
        ("10", "($10.00)", False, False),
        ("10", "10,00-", False, False),
        ("10", "10,00 D", False, False),
        ("10", "10,00 mil", False, False),
        ("1234", "1.234,00 mil", False, False),
        ("10", "\u221210,00 kr", False, False),
        ("10", "\u221210,00", False, False),
        ("10", "\u201310", False, False),
        ("10", "(10)", False, False),
        ("10", "10-", False, False),
        ("10,5", "(10,50)", False, False),
        ("-10", "\u221210,00", False, True),
        ("10", "$10.00", False, True),
        ("10", "10,00\u00a0€", False, True),
        ("1234", "R$ 1.234,00", False, True),
        ("12345", "OS-12345", False, False),
    ],
)
def test_the_typed_text_and_the_field_value(
    text: str, value: str, number: bool, matches: bool
) -> None:
    assert _typed_value_matches(text, value, browser=None, number=number) is matches


# -- rodada 11b: a referência dos campos, o vigia e as caixas já marcadas -------------------

_LOG_POST = _post("document.getElementById('log').value+';'").replace('"', "&quot;")

#: O CEP preenche o logradouro 150 ms depois de mudar, enquanto o robô espera o aviso; a
#: página tem uma aba, uma lista e uma caixa que um passo de leitura pode usar no meio.
_CEP_TABS = _page(
    "<label for='cep'>CEP</label><input id='cep'>"
    "<label for='log'>Logradouro</label><input id='log'>"
    "<label for='num'>Número</label><input id='num'>"
    "<label for='tipo'>Tipo</label><select id='tipo'><option>Casa</option>"
    "<option>Apartamento</option></select>"
    "<p id='ok' hidden>Endereço conferido</p>"
    "<button type='button' onclick=\"document.getElementById('pg').hidden=false\">Pagamento</button>"
    "<section id='pg' hidden><p>Pagamento</p></section>"
    f"<button type='button' onclick=\"{_LOG_POST}\">Salvar</button>"
    "<script>document.getElementById('cep').addEventListener('change',()=>{"
    "setTimeout(()=>{document.getElementById('log').value='Rua do CEP'},150);"
    "setTimeout(()=>{document.getElementById('ok').hidden=false},600)})</script>"
)


def _wait_text(step_id: str, text: str) -> dict[str, Any]:
    return {
        "kind": "wait_for",
        "id": step_id,
        "title": "Esperar o aviso",
        "target": _target("aviso", {"by": "text", "value": text, "exact": True}),
        "state": "visible",
        "timeout_ms": 5_000,
    }


@pytest.mark.parametrize("middle", ["clique", "tecla", "escolha", "endereco"])
async def test_a_read_step_after_a_wait_never_makes_the_page_change_the_reference(
    run_robot: RobotRunner, srv: _Server, middle: str
) -> None:
    # Z1: a resposta do CEP chega durante a espera; um passo de leitura que age (uma aba,
    # um Tab, uma escolha, um #) vem antes do Salvar. Só ganham referência nova os campos
    # que ainda a tinham logo antes de o passo agir: o logradouro mudado fica com a
    # antiga, e o Salvar falha sem gravar.
    srv.app = _api_app(srv, {"/p": _CEP_TABS})
    middle_step: dict[str, Any] = {
        "clique": _click("aba", "Pagamento", effect="read"),
        "tecla": {
            "kind": "press",
            "id": "tab",
            "title": "Teclar Tab",
            "effect": "read",
            "key": "Tab",
            "target": _target("campo Número", {"by": "label", "value": "Número", "exact": True}),
        },
        "escolha": {
            "kind": "select",
            "id": "tipo",
            "title": "Escolher o tipo",
            "effect": "read",
            "option": "Apartamento",
            "target": _target("lista Tipo", {"by": "label", "value": "Tipo", "exact": True}),
        },
        "endereco": _nav(srv.base, "/p#pagamento", "ir_pagamento"),
    }[middle]
    manifest = _manifest(
        [
            _nav(srv.base, "/p"),
            _fill("digitar_cep", "CEP", "01001000"),
            _fill("digitar_log", "Logradouro", "Praça da Sé"),
            _fill("digitar_num", "Número", "100"),
            _wait_text("esperar", "Endereço conferido"),
            middle_step,
            _save(),
        ]
    )
    outcome = await _run(run_robot, manifest)
    _wrote(outcome, srv, RunState.FAILED, "")
    assert outcome.result.failed_step_id == "salvar"
    assert outcome.result.error_code == ErrorCode.ACAO_FALHOU


def _cover_cep(kind: str) -> dict[str, str]:
    """O CEP põe uma camada ("buscando CEP") ou um diálogo modal por cima da página e, 1 s
    depois, preenche o logradouro e tira a camada."""
    if kind == "camada":
        show = (
            "const c=document.createElement('div');c.id='cobre';c.textContent='Buscando CEP';"
            "c.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:10';"
            "document.body.appendChild(c);"
        )
        hide = "document.getElementById('cobre').remove();"
    else:
        show = "document.getElementById('d').showModal();"
        hide = "document.getElementById('d').close();"
    later = f"setTimeout(()=>{{document.getElementById('log').value='Rua do CEP';{hide}}},1000);"
    page = _page(
        "<label for='log'>Logradouro</label><input id='log'>"
        "<label for='num'>Número</label><input id='num'>"
        "<label for='cep'>CEP</label><input id='cep'>"
        "<dialog id='d'><p>Buscando CEP</p></dialog>"
        f"<button type='button' onclick=\"{_LOG_POST}\">Salvar</button>"
        "<script>let feito=false;document.getElementById('cep').addEventListener('input',(e)=>{"
        f"if(feito||e.target.value.length<8)return;feito=true;{show}{later}}})</script>"
    )
    return {"/p": page}


@pytest.mark.parametrize("kind", ["camada", "modal"])
@pytest.mark.parametrize("effect", ["write", "irreversible"])
async def test_a_field_the_page_changes_while_the_click_waits_for_a_cover_is_never_sent(
    run_robot: RobotRunner, srv: _Server, kind: str, effect: str
) -> None:
    # Z2: o Salvar espera a camada sair (o clique vai sem force); nesse tempo a página
    # troca o logradouro. O vigia confere os campos digitados no primeiro evento do clique.
    srv.app = _api_app(srv, _cover_cep(kind))
    manifest = _manifest(
        [
            _nav(srv.base, "/p"),
            _fill("digitar_log", "Logradouro", "Praça da Sé"),
            _fill("digitar_num", "Número", "100"),
            _fill("digitar_cep", "CEP", "01001000"),
            _click("salvar", "Salvar", effect=effect, timeout=5_000),
        ]
    )
    outcome = await _run(run_robot, manifest)
    state = RunState.FAILED if effect == "write" else RunState.INDETERMINATE
    _wrote(outcome, srv, state, "")
    assert outcome.result.error_code == ErrorCode.ACAO_FALHOU


_ALREADY = _page(
    "<label><input type='checkbox' id='a' checked "
    "onchange=\"fetch('/api',{method:'POST',body:'A'+this.checked+';'})\"> Aceito</label>"
    "<label><input type='checkbox' id='n' "
    "onchange=\"fetch('/api',{method:'POST',body:'N'+this.checked+';'})\"> Notificar</label>"
    f"<button type='button' onclick=\"{_post(chr(39) + 'S;' + chr(39))}\">Salvar</button>"
)


@pytest.mark.parametrize(("label", "checked"), [("Aceito", True), ("Notificar", False)])
@pytest.mark.parametrize("effect", ["write", "irreversible"])
async def test_a_box_already_as_it_should_be_is_not_a_refusal(
    run_robot: RobotRunner, srv: _Server, label: str, checked: bool, effect: str
) -> None:
    # Z3: o Playwright não clica numa caixa que já está como deve ficar, e o vigia não vê
    # nenhum evento: isso não é recusa.
    srv.app = _api_app(srv, {"/p": _ALREADY})
    step = {
        "kind": "check",
        "id": "marcar",
        "title": f"Marcar {label}",
        "effect": effect,
        "timeout_ms": 3_000,
        "checked": checked,
        "target": _target(f"caixa {label}", {"by": "label", "value": label, "exact": True}),
    }
    outcome = await _run(run_robot, _manifest([_nav(srv.base, "/p"), step, _save()]))
    _wrote(outcome, srv, RunState.SUCCEEDED, "S;")


def _cart_with_notes(n: int) -> dict[str, str]:
    rows = "".join(
        f"<tr><td>P{i}</td><td><input aria-label='Quantidade' value=''></td><td><button "
        "type='button' onclick=\"const q=this.closest('tr').querySelector('input');"
        f"fetch('/api',{{method:'POST',body:'P{i}='+q.value+';'}})\">Salvar</button></td></tr>"
        for i in range(n)
    )
    return {
        "/itens": _page(
            "<iframe id='obsf' src='/obs' style='width:400px;height:80px'></iframe>"
            "<table aria-label='Itens'><thead><tr><th>produto</th><th>qtd</th><th>acao</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
        ),
        "/obs": _page("<label for='o'>Obs</label><input id='o'>"),
    }


async def test_fields_typed_in_another_frame_are_read_in_their_own_document(
    run_robot: RobotRunner, srv: _Server
) -> None:
    # Z9: um campo digitado num iframe e as quantidades da página principal: a releitura
    # antes de cada Salvar lê cada campo no documento dele (antes, o do iframe vinha vazio e
    # a primeira gravação falhava).
    srv.app = _api_app(srv, _cart_with_notes(3))
    obs = _fill("digitar_obs", "Obs", "observação")
    obs["target"]["frames"] = ["#obsf"]
    qty = _fill("digitar_qtd", "Quantidade", "{{ item.qtd }}", effect="write")
    qty["target"] = _target(
        "quantidade",
        {"by": "role", "value": "textbox", "name": "Quantidade", "exact": True},
        **_row("{{ item.produto }}"),
    )
    save = _click("salvar", "Salvar", key="{{ item.produto }}", effect="write")
    manifest = _manifest(
        [
            _nav(srv.base, "/itens"),
            obs,
            {
                "kind": "for_each",
                "id": "cada",
                "title": "Para cada",
                "items": "inputs.itens",
                "steps": [qty, save],
            },
        ],
        inputs=[
            {
                "name": "itens",
                "title": "itens",
                "type": "list",
                "item_fields": [
                    {"name": "produto", "title": "produto", "type": "string"},
                    {"name": "qtd", "title": "qtd", "type": "string"},
                ],
            }
        ],
    )
    items = [{"produto": f"P{i}", "qtd": str(i + 1)} for i in range(3)]
    outcome = await _run(run_robot, manifest, inputs={"itens": items})
    _wrote(outcome, srv, RunState.SUCCEEDED, "P0=1;P1=2;P2=3;")


# -- campos digitados noutro documento que o do alvo -----------------------------------------


def _cross_document_app(
    srv: _Server, field: str, cover: str, change: bool, *, delay_ms: int = 0, last_ms: int = 1000
) -> App:
    """O logradouro fica na página principal (``principal``) ou noutro iframe (``outro``);
    o CEP e o Salvar, no iframe ``#f``. ``delay_ms`` depois do CEP, a página põe uma camada
    de "buscando CEP" por cima do Salvar (no próprio iframe, ``iframe``, ou na página de
    cima, ``pai``), ou deixa o corpo do iframe sem eventos do mouse (``pe_corpo``), ou o
    iframe inerte (``inerte_pai``), por ``last_ms``; ``change`` troca o logradouro antes
    de tirar a camada."""
    log_doc = "parent.document" if field == "principal" else "parent.frames['g'].document"
    if cover == "iframe":
        show = (
            "const c=document.createElement('div');c.id='cobre';c.textContent='Buscando CEP';"
            "c.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:10';"
            "document.body.appendChild(c);"
        )
        hide = "document.getElementById('cobre').remove();"
    elif cover == "pai":
        show = "parent.document.getElementById('capa').hidden=false;"
        hide = "parent.document.getElementById('capa').hidden=true;"
    elif cover == "pe_corpo":
        show = "document.body.style.pointerEvents='none';"
        hide = "document.body.style.pointerEvents='';"
    else:
        show = "parent.document.getElementById('f').inert=true;"
        hide = "parent.document.getElementById('f').inert=false;"
    swap = f"{log_doc}.getElementById('log').value='Rua do CEP';" if change else ""
    form = _page(
        "<label for='cep'>CEP</label><input id='cep'>"
        "<button type='button' onclick=\"fetch('/api',{method:'POST',body:"
        f"{log_doc}.getElementById('log').value+';'}})\">Salvar</button>"
        "<script>let feito=false;document.getElementById('cep').addEventListener('input',(e)=>{"
        "if(feito||e.target.value.length<8)return;feito=true;setTimeout(()=>{"
        f"{show}setTimeout(()=>{{{swap}{hide}}},{last_ms});}},{delay_ms});}})</script>"
    )
    log_input = "<label for='log'>Logradouro</label><input id='log'>"
    host = _page(
        "<div id='capa' hidden style='position:fixed;inset:0;z-index:10;"
        "background:rgba(0,0,0,.2)'></div>"
        + (log_input if field == "principal" else "")
        + "<iframe id='f' name='f' src='/form' style='width:600px;height:200px'></iframe>"
        + (
            "<iframe id='g' name='g' src='/log' style='width:600px;height:100px'></iframe>"
            if field != "principal"
            else ""
        )
    )
    return _api_app(srv, {"/host": host, "/form": form, "/log": _page(log_input)})


def _cross_document_robot(srv: _Server, field: str, effect: str = "write") -> Manifest:
    log = _fill("digitar_log", "Logradouro", "Praça da Sé")
    if field != "principal":
        log["target"]["frames"] = ["#g"]
    cep = _fill("digitar_cep", "CEP", "01001000")
    cep["target"]["frames"] = ["#f"]
    save = _click("salvar", "Salvar", effect=effect, timeout=6_000)
    save["target"]["frames"] = ["#f"]
    return _manifest([_nav(srv.base, "/host"), log, cep, save])


@pytest.mark.parametrize("cover", ["iframe", "pai", "pe_corpo", "inerte_pai"])
@pytest.mark.parametrize("field", ["principal", "outro"])
async def test_a_field_of_another_document_changed_while_the_click_waits_is_never_sent(
    run_robot: RobotRunner, srv: _Server, field: str, cover: str
) -> None:
    # N4: o logradouro está noutro documento que o Salvar, e o vigia do clique só lê os
    # campos do documento do alvo. A releitura vem depois de o Salvar ficar livre (também
    # de uma camada sem eventos do mouse ou inerte, que a espera conta como coberta).
    srv.app = _cross_document_app(srv, field, cover, change=True)
    outcome = await _run(run_robot, _cross_document_robot(srv, field))
    _wrote(outcome, srv, RunState.FAILED, "")
    assert outcome.result.error_code == ErrorCode.ACAO_FALHOU


@pytest.mark.parametrize("cover", ["iframe", "pai", "pe_corpo"])
@pytest.mark.parametrize("field", ["principal", "outro"])
async def test_a_cover_over_the_click_with_fields_in_another_document_only_delays_it(
    run_robot: RobotRunner, srv: _Server, field: str, cover: str
) -> None:
    # A camada dura 2,6 s: a espera antes da releitura a vê, e o clique vai logo depois.
    srv.app = _cross_document_app(srv, field, cover, change=False, last_ms=2600)
    outcome = await _run(run_robot, _cross_document_robot(srv, field, effect="irreversible"))
    _wrote(outcome, srv, RunState.SUCCEEDED, "Praça da Sé;")


OTHER_HOST = "outro.rapa.test"


def _cross_origin_app(srv: _Server) -> App:
    """O Salvar fica num iframe de outra origem; a página principal o cobre por 2,5 s quando
    o CEP é digitado e, no fim, troca o logradouro e tira a capa (o iframe não vê nada
    disso)."""
    form = _page(
        "<label for='cep'>CEP</label><input id='cep'>"
        f"<button type='button' onclick=\"{_post(chr(39) + 'S;' + chr(39))}\">Salvar</button>"
        "<script>let feito=false;document.getElementById('cep').addEventListener('input',(e)=>{"
        "if(feito||e.target.value.length<8)return;feito=true;parent.postMessage('cep','*')})"
        "</script>"
    )
    host = _page(
        "<div id='capa' hidden style='position:fixed;inset:0;z-index:10;"
        "background:rgba(0,0,0,.2)'></div>"
        "<label for='log'>Logradouro</label><input id='log'>"
        f"<iframe id='f' src='http://{OTHER_HOST}:{srv.port}/form' "
        "style='width:600px;height:200px'></iframe>"
        "<script>addEventListener('message',()=>{const c=document.getElementById('capa');"
        "c.hidden=false;setTimeout(()=>{document.getElementById('log').value='Rua do CEP';"
        "c.hidden=true},2500)})</script>"
    )
    return _api_app(srv, {"/host": host, "/form": form})


async def test_a_click_that_waits_for_another_origin_with_fields_elsewhere_is_refused(
    run_robot: RobotRunner, srv: _Server
) -> None:
    # O Salvar está num iframe de outra origem, e a capa da página principal por cima dele
    # não pode ser vista de dentro: o Playwright espera, e o primeiro evento chega mais de
    # 2 s depois do vigia. O logradouro, da página principal, pode ter mudado nessa espera.
    srv.app = _cross_origin_app(srv)
    log = _fill("digitar_log", "Logradouro", "Praça da Sé")
    cep = _fill("digitar_cep", "CEP", "01001000")
    cep["target"]["frames"] = ["#f"]
    save = _click("salvar", "Salvar", effect="write", timeout=8_000)
    save["target"]["frames"] = ["#f"]
    manifest = _manifest(
        [_nav(srv.base, "/host"), log, cep, save], allowed_domains=[HOST, OTHER_HOST]
    )
    outcome = await _run(run_robot, manifest)
    _wrote(outcome, srv, RunState.FAILED, "")
    assert outcome.result.error_code == ErrorCode.ACAO_FALHOU
    assert "pode ter mudado" in (outcome.result.error_message or "")


@pytest.mark.parametrize("delay_ms", [0, 60, 150, 300])
async def test_a_cover_that_comes_after_the_wait_never_sends_another_value(
    run_robot: RobotRunner, srv: _Server, delay_ms: int
) -> None:
    # A camada chega um pouco depois do CEP, talvez depois da espera antes da releitura:
    # o Playwright espera por ela, e o vigia recusa o primeiro evento que demorou. Ou o
    # clique foi antes dela, com o logradouro digitado. Nunca com o da página.
    srv.app = _cross_document_app(srv, "principal", "iframe", change=True, delay_ms=delay_ms)
    outcome = await _run(run_robot, _cross_document_robot(srv, "principal"))
    sent = "".join(srv.log)
    assert "Rua do CEP" not in sent, outcome.result.error_message
    if outcome.result.state is RunState.SUCCEEDED:
        assert sent == "Praça da Sé;"
    else:
        assert sent == ""
        assert outcome.result.error_code == ErrorCode.ACAO_FALHOU


def _scaled_iframe_app(srv: _Server) -> App:
    """O Salvar num iframe em escala 0,5; um painel da página principal fica onde o ponto do
    Salvar cairia sem a escala (e não cobre o Salvar de verdade)."""
    form = _page(
        "<button type='button' style='position:absolute;left:400px;top:200px' onclick=\""
        "fetch('/api',{method:'POST',body:parent.document.getElementById('log').value+';'})"
        '">Salvar</button>'
    )
    host = _page(
        "<iframe id='f' src='/form' style='position:absolute;left:0;top:0;width:600px;"
        "height:300px;transform:scale(.5);transform-origin:0 0'></iframe>"
        "<div style='position:absolute;left:380px;top:180px;width:160px;height:100px;"
        "background:#ddd'>Painel</div>"
        "<div style='position:absolute;left:0;top:400px'>"
        "<label for='log'>Logradouro</label><input id='log'></div>"
    )
    return _api_app(srv, {"/host": host, "/form": form})


async def test_a_click_in_a_scaled_iframe_with_fields_elsewhere_is_not_held_back(
    run_robot: RobotRunner, srv: _Server
) -> None:
    # Num iframe transformado, a espera antes da releitura não traduz o ponto e deixa a
    # conferência para o Playwright: o clique vai logo.
    srv.app = _scaled_iframe_app(srv)
    save = _click("salvar", "Salvar", effect="write", timeout=6_000)
    save["target"]["frames"] = ["#f"]
    manifest = _manifest(
        [_nav(srv.base, "/host"), _fill("digitar_log", "Logradouro", "Praça da Sé"), save]
    )
    outcome = await _run(run_robot, manifest)
    _wrote(outcome, srv, RunState.SUCCEEDED, "Praça da Sé;")


_STICKY_HEADER = _page(
    "<div style='position:sticky;top:0;height:160px;background:#fff;z-index:5'>"
    "<iframe id='o' src='/obs' style='width:400px;height:120px'></iframe></div>"
    "<div style='height:1500px'></div>"
    f"<button id='s' type='button' onclick=\"{_post(chr(39) + 'S;' + chr(39))}\">Salvar</button>"
    "<div style='height:1500px'></div>"
    "<script>addEventListener('load',()=>{const b=document.getElementById('s');"
    "scrollTo(0,b.getBoundingClientRect().top+scrollY-60)})</script>"
)


async def test_a_button_under_a_sticky_header_with_fields_elsewhere_is_scrolled_free(
    run_robot: RobotRunner, srv: _Server
) -> None:
    # O Salvar fica embaixo de um cabeçalho fixo (onde está o iframe com o campo digitado):
    # a espera antes da releitura rola o Salvar para a janela de novo, como o Playwright.
    srv.app = _api_app(
        srv, {"/p": _STICKY_HEADER, "/obs": _page("<label for='o'>Obs</label><input id='o'>")}
    )
    obs = _fill("digitar_obs", "Obs", "observação")
    obs["target"]["frames"] = ["#o"]
    manifest = _manifest(
        [_nav(srv.base, "/p"), obs, _click("salvar", "Salvar", effect="write", timeout=6_000)]
    )
    outcome = await _run(run_robot, manifest)
    _wrote(outcome, srv, RunState.SUCCEEDED, "S;")

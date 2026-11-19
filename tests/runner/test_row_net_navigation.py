"""A rede contra ações repetidas numa linha segue a página até o fim da volta.

Uma ação que grava numa linha escolhida pelo texto conta em todas as páginas por onde
a página principal passa até o fim da volta da repetição em que ela agiu, mesmo
quando quem leva a página a outro endereço é um passo seguinte da mesma volta (a
confirmação de um modal, o botão "Cancelar selecionadas", um link do robô). Na volta
seguinte, um item repetido da tabela lida não age de novo na mesma linha.

As páginas vêm de um servidor HTTP de verdade (redirecionamentos 303 e ``meta
refresh`` de verdade, que o ``route.fulfill`` do Playwright não segue), num domínio de
teste com porta. O registro do que gravou é o do servidor: cada pedido que cancelou.
"""

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
from rapa.core.events import RunEventKind
from rapa.core.robot import Manifest
from rapa.runner.errors import ErrorCode
from rapa.runner.ports import needs_ack
from tests.runner.support import Outcome, RobotRunner, assert_seq

pytestmark = pytest.mark.browser

HOST = "paginas.rapa.test"

#: Responde a um pedido: (método, caminho com consulta, campos do formulário) ->
#: (status, cabeçalhos, corpo).
type _App = Callable[[str, str, dict[str, list[str]]], tuple[int, dict[str, str], str]]


class _Server:
    """Servidor HTTP de verdade, com o registro, do lado dele, de cada cancelamento."""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.app: _App = lambda _method, _path, _form: (404, {}, "")
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def _serve(self, method: str) -> None:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length).decode() if length else ""
                form = parse_qs(raw, keep_blank_values=True)
                form["_corpo"] = [raw]
                status, headers, body = server.app(method, self.path, form)
                data = body.encode()
                self.send_response(status)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("cache-control", "no-store")
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._serve("GET")

            def do_POST(self) -> None:
                self._serve("POST")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://{HOST}:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def server() -> Iterator[_Server]:
    running = _Server()
    yield running
    running.close()


def _page(body: str, head: str = "") -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Notas</title>"
        f"{head}</head><body>{body}</body></html>"
    )


def _table(rows: str) -> str:
    return (
        "<table aria-label='Notas'><thead><tr><th>numero</th><th>acao</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


# O relatório (outra página) tem a nota 120 duas vezes.
_RELATORIO = _page(_table("<tr><td>120</td><td>ok</td></tr>" * 2))
_NUMEROS = ("120", "7")


def _manifest(steps: list[dict[str, Any]], **extra: Any) -> Manifest:
    return Manifest.model_validate(
        {
            "name": "robo_de_teste",
            "title": "Robô de teste",
            "description": "Robô montado pelos testes da rede contra ações repetidas.",
            "allowed_domains": [HOST],
            "steps": steps,
            **extra,
        }
    )


def _navigate(base: str, path: str, step_id: str) -> dict[str, Any]:
    return {"kind": "navigate", "id": step_id, "title": "Abrir a página", "url": f"{base}{path}"}


def _click(
    step_id: str,
    name: str,
    *,
    row: bool = False,
    effect: str = "irreversible",
    navigation: bool = False,
    role: str = "button",
) -> dict[str, Any]:
    target: dict[str, Any] = {
        "intent": f"{role} {name}",
        "locators": [{"by": "role", "value": role, "name": name, "exact": True}],
    }
    if row:
        target |= {"within": {"by": "role", "value": "row"}, "within_has_text": "{{ item.numero }}"}
    return {
        "kind": "click",
        "id": step_id,
        "title": f"Clicar em {name}",
        "effect": effect,
        "timeout_ms": 3_000,
        "expect_navigation": navigation,
        "target": target,
    }


def _check_row(step_id: str) -> dict[str, Any]:
    return {
        "kind": "check",
        "id": step_id,
        "title": "Marcar a nota",
        "effect": "write",
        "timeout_ms": 3_000,
        "target": {
            "intent": "caixa de seleção da nota",
            "locators": [{"by": "role", "value": "checkbox", "name": "Selecionar", "exact": True}],
            "within": {"by": "role", "value": "row"},
            "within_has_text": "{{ item.numero }}",
        },
    }


def _wait_url(fragment: str) -> dict[str, Any]:
    return {
        "kind": "wait_for",
        "id": "esperar",
        "title": "Esperar o endereço",
        "url_contains": fragment,
        "timeout_ms": 5_000,
    }


def _read_notes() -> dict[str, Any]:
    return {
        "kind": "extract_table",
        "id": "ler",
        "title": "Ler as notas",
        "target": {
            "intent": "notas",
            "locators": [{"by": "role", "value": "table", "name": "Notas"}],
        },
        "into": "notas",
        "columns": ["numero", "acao"],
    }


def _for_each(items: str, steps: list[dict[str, Any]], step_id: str) -> dict[str, Any]:
    return {"kind": "for_each", "id": step_id, "title": "Para cada", "items": items, "steps": steps}


def _from_report(base: str, body: list[dict[str, Any]]) -> Manifest:
    """Lê as notas do relatório e, para cada uma, faz ``body`` na página das notas."""
    return _manifest(
        [
            _navigate(base, "/relatorio", "abrir_relatorio"),
            _read_notes(),
            _navigate(base, "/notas", "abrir_notas"),
            _for_each("vars.notas", body, "cada_nota"),
        ]
    )


async def _settle(context: BrowserContext) -> None:
    """Dá tempo para um pedido que a página ainda faria (o registro é do servidor)."""
    if context.pages:
        try:
            await context.pages[0].wait_for_timeout(600)
        except PlaywrightError:
            pass


async def _run(run_robot: RobotRunner, manifest: Manifest, **options: Any) -> Outcome:
    assert policy.check(manifest) == []
    return await run_robot(manifest, after_run=_settle, **options)


def _assert_refused_once(
    outcome: Outcome, server: _Server, step_id: str, *, irreversible: bool
) -> None:
    """A nota 120 foi cancelada uma vez só, e o item repetido foi recusado antes de agir."""
    result = outcome.result
    assert server.writes == ["C120"], result.error_message  # nunca duas vezes
    assert result.error_code == ErrorCode.ELEMENTO_AMBIGUO, result.error_message
    assert result.failed_step_id == step_id
    assert "já agiu" in (result.error_message or "")
    assert result.state is (RunState.INDETERMINATE if irreversible else RunState.FAILED)
    [failure] = outcome.of(RunEventKind.STEP_FAILED, step_id)
    assert failure.data["passo_anterior"] == step_id
    assert failure.data["repeticao"] == "cada_nota"
    assert failure.data["iteracao_anterior"] == 0
    assert len(outcome.of(RunEventKind.STEP_SUCCEEDED, step_id)) == 1
    assert_seq(outcome.events)


# -- o passo seguinte da mesma volta leva a página a outro endereço ------------------------


def _modal_app(server: _Server, response: str) -> _App:
    """O "Cancelar" da linha abre um modal; o "Confirmar" dele envia o POST."""
    rows = "".join(
        f"<tr><td>{n}</td><td><button type='button' onclick=\"abrir('{n}')\">Cancelar"
        "</button></td></tr>"
        for n in _NUMEROS
    )
    modal = (
        "<div role='dialog' aria-label='Confirmação' id='modal' hidden>"
        "<form method='post' action='/notas/cancelar'><input type='hidden' name='nota' id='nota'>"
        "<button type='submit'>Confirmar</button></form></div>"
        "<script>function abrir(n) { document.getElementById('nota').value = n; "
        "document.getElementById('modal').hidden = false; }</script>"
    )
    notas = _page(_table(rows) + modal)

    def app(method: str, path: str, form: dict[str, list[str]]) -> tuple[int, dict[str, str], str]:
        if urlsplit(path).path == "/relatorio":
            return 200, {}, _RELATORIO
        if method == "POST":
            server.writes.extend(f"C{n}" for n in form.get("nota", []))
            if response == "volta_para_notas":
                return 303, {"location": "/notas"}, ""
            return 200, {}, notas  # a resposta do POST mostra a mesma tabela
        return 200, {}, notas

    return app


@pytest.mark.parametrize("row_effect", ["write", "irreversible"])
@pytest.mark.parametrize("response", ["mesma_tabela", "volta_para_notas"])
async def test_confirmation_in_another_step_never_acts_twice(
    run_robot: RobotRunner, server: _Server, response: str, row_effect: str
) -> None:
    # O clique na linha só abre o modal; quem envia o POST é o passo seguinte, e a
    # resposta mostra a mesma tabela em /notas/cancelar. A segunda nota 120 está nesse
    # endereço, e mesmo assim é recusada antes de agir.
    server.app = _modal_app(server, response)
    manifest = _from_report(
        server.base,
        [
            _click("cancelar", "Cancelar", row=True, effect=row_effect),
            _click("confirmar", "Confirmar", navigation=True),
        ],
    )
    outcome = await _run(run_robot, manifest)
    _assert_refused_once(outcome, server, "cancelar", irreversible=True)
    assert len(outcome.of(RunEventKind.STEP_SUCCEEDED, "confirmar")) == 1


def _selection_app(server: _Server) -> _App:
    """Cada linha tem uma caixa "Selecionar"; "Cancelar selecionadas" envia o POST."""
    rows = "".join(
        f"<tr><td>{n}</td><td><input type='checkbox' name='nota' value='{n}' "
        "aria-label='Selecionar'></td></tr>"
        for n in _NUMEROS
    )
    notas = _page(
        "<form method='post' action='/notas/cancelar'>"
        + _table(rows)
        + "<button type='submit'>Cancelar selecionadas</button></form>"
    )

    def app(method: str, path: str, form: dict[str, list[str]]) -> tuple[int, dict[str, str], str]:
        if urlsplit(path).path == "/relatorio":
            return 200, {}, _RELATORIO
        if method == "POST":
            server.writes.extend(f"C{n}" for n in form.get("nota", []))
        return 200, {}, notas

    return app


async def test_bulk_action_after_selecting_the_row_never_acts_twice(
    run_robot: RobotRunner, server: _Server
) -> None:
    server.app = _selection_app(server)
    manifest = _from_report(
        server.base,
        [
            _check_row("marcar"),
            _click("cancelar_selecionadas", "Cancelar selecionadas", navigation=True),
        ],
    )
    outcome = await _run(run_robot, manifest)
    _assert_refused_once(outcome, server, "marcar", irreversible=True)


async def test_selection_in_an_inner_loop_counts_for_the_outer_iteration(
    run_robot: RobotRunner, server: _Server
) -> None:
    # Cada lote marca as notas lidas (repetição de dentro) e depois clica em "Cancelar
    # selecionadas" (fora dela). A resposta mostra a mesma tabela em /notas/cancelar, e
    # o segundo lote começa nela: a nota 120 não é marcada e cancelada de novo.
    server.app = _selection_app(server)
    relatorio = _page(_table("<tr><td>120</td><td>ok</td></tr>"))
    notas_app = server.app

    def app(method: str, path: str, form: dict[str, list[str]]) -> tuple[int, dict[str, str], str]:
        if urlsplit(path).path == "/relatorio":
            return 200, {}, relatorio
        return notas_app(method, path, form)

    server.app = app
    manifest = _manifest(
        [
            _navigate(server.base, "/relatorio", "abrir_relatorio"),
            _read_notes(),
            _navigate(server.base, "/notas", "abrir_notas"),
            _for_each(
                "inputs.lotes",
                [
                    _for_each("vars.notas", [_check_row("marcar")], "cada_nota"),
                    _click("cancelar_selecionadas", "Cancelar selecionadas", navigation=True),
                ],
                "cada_lote",
            ),
        ],
        inputs=[
            {
                "name": "lotes",
                "title": "Lotes",
                "type": "list",
                "item_fields": [{"name": "lote", "title": "Lote", "type": "string"}],
            }
        ],
    )
    outcome = await _run(run_robot, manifest, inputs={"lotes": [{"lote": "1"}, {"lote": "2"}]})
    result = outcome.result
    assert server.writes == ["C120"], result.error_message
    assert result.state is RunState.INDETERMINATE
    assert result.error_code == ErrorCode.ELEMENTO_AMBIGUO
    assert result.failed_step_id == "marcar"
    [failure] = outcome.of(RunEventKind.STEP_FAILED, "marcar")
    assert failure.data["repeticao"] == "cada_lote"
    assert failure.data["iteracao_anterior"] == 0


def _script_app(server: _Server, how: str) -> _App:
    """O "Cancelar" da linha faz um fetch e, 300 ms depois, leva a página a outro endereço."""
    go = {
        "href": "location.href = '/notas/lista'",
        "replace": "location.replace('/notas/lista')",
        "push": "history.pushState(null, '', '/notas/cancelada/' + n)",
        "replacestate": "history.replaceState(null, '', '/notas/cancelada/' + n)",
        "submit": "document.getElementById('enviar').submit()",
    }[how]
    rows = "".join(
        f"<tr><td>{n}</td><td><button type='button' onclick=\"cancelar('{n}')\">Cancelar"
        "</button></td></tr>"
        for n in _NUMEROS
    )
    script = (
        "<form id='enviar' method='post' action='/notas/feito'></form>"
        "<div id='aviso'>Aviso <button type='button' onclick='this.parentNode.hidden = true'>"
        "Fechar aviso</button></div>"
        "<script>function cancelar(n) { fetch('/api/cancelar', {method: 'POST', body: n})"
        f".then(() => setTimeout(() => {{ {go} }}, 300)); }}</script>"
    )
    notas = _page(_table(rows) + script)

    def app(method: str, path: str, form: dict[str, list[str]]) -> tuple[int, dict[str, str], str]:
        where = urlsplit(path).path
        if where == "/relatorio":
            return 200, {}, _RELATORIO
        if method == "POST" and where == "/api/cancelar":
            server.writes.append(f"C{form['_corpo'][0]}")
            return 200, {}, "ok"
        return 200, {}, notas

    return app


@pytest.mark.parametrize("how", ["href", "replace", "push", "replacestate", "submit"])
@pytest.mark.parametrize("body", ["espera_o_endereco", "fecha_o_aviso_e_espera"])
async def test_script_navigation_after_the_action_never_acts_twice(
    run_robot: RobotRunner, server: _Server, how: str, body: str
) -> None:
    # A navegação vem do script da página, depois do fetch, enquanto o robô faz outro
    # passo (que grava, mas não numa linha) ou espera o endereço.
    server.app = _script_app(server, how)
    fragment = {"push": "/notas/cancelada", "replacestate": "/notas/cancelada"}.get(
        how, "/notas/feito" if how == "submit" else "/notas/lista"
    )
    steps = [_click("cancelar", "Cancelar", row=True)]
    if body == "fecha_o_aviso_e_espera":
        steps.append(_click("fechar", "Fechar aviso", effect="write"))
    steps.append(_wait_url(fragment))
    outcome = await _run(run_robot, _from_report(server.base, steps))
    _assert_refused_once(outcome, server, "cancelar", irreversible=True)


def _forms_app(
    server: _Server, *, location: str | None, head: str = "", link: bool = False
) -> _App:
    """Cada linha tem um formulário que cancela a nota por POST em /notas/cancelar.

    A resposta redireciona (303 de verdade) para ``location`` ou, sem ele, mostra a
    mesma tabela, com ``head`` (um ``meta refresh``, por exemplo).
    """
    rows = "".join(
        f"<tr><td>{n}</td><td><form method='post' action='/notas/cancelar'>"
        f"<input type='hidden' name='nota' value='{n}'><button>Cancelar</button></form></td></tr>"
        for n in _NUMEROS
    )
    menu = "<a href='/notas/lista'>Ver lista</a>" if link else ""
    notas = _page(menu + _table(rows))
    landing = _page(menu + _table(rows), head)

    def app(method: str, path: str, form: dict[str, list[str]]) -> tuple[int, dict[str, str], str]:
        if urlsplit(path).path == "/relatorio":
            return 200, {}, _RELATORIO
        if method == "POST":
            server.writes.extend(f"C{n}" for n in form.get("nota", []))
            if location is not None:
                return 303, {"location": location}, ""
            return 200, {}, landing
        return 200, {}, notas

    return app


@pytest.mark.parametrize("how", ["link", "navigate"])
async def test_a_page_the_robot_opens_after_the_action_is_still_the_same_row(
    run_robot: RobotRunner, server: _Server, how: str
) -> None:
    # Depois de cancelar (o POST volta para /notas), o robô vai, na mesma volta, para
    # /notas/lista, que mostra a mesma tabela. A volta seguinte começa nela: o que a
    # volta fez depois de agir ainda é a linha onde ela agiu.
    server.app = _forms_app(server, location="/notas", link=True)
    second = (
        _click("ver_lista", "Ver lista", role="link", effect="read", navigation=True)
        if how == "link"
        else _navigate(server.base, "/notas/lista", "ver_lista")
    )
    manifest = _from_report(
        server.base, [_click("cancelar", "Cancelar", row=True, navigation=True), second]
    )
    outcome = await _run(run_robot, manifest)
    _assert_refused_once(outcome, server, "cancelar", irreversible=True)


@pytest.mark.parametrize("navigation", [True, False])
@pytest.mark.parametrize(
    "location", ["/notas/cancelada", "/Notas/", "/notas;jsessionid=ab12", "/notas/index.php"]
)
async def test_real_redirect_after_the_action_never_acts_twice(
    run_robot: RobotRunner, server: _Server, location: str, navigation: bool
) -> None:
    server.app = _forms_app(server, location=location)
    manifest = _from_report(
        server.base, [_click("cancelar", "Cancelar", row=True, navigation=navigation)]
    )
    outcome = await _run(run_robot, manifest)
    _assert_refused_once(outcome, server, "cancelar", irreversible=True)


@pytest.mark.parametrize(
    ("delay", "target", "navigation", "wait"),
    [
        ("0", "/notas/lista", False, True),
        ("0", "/notas/lista", True, True),
        ("1", "/notas/lista", False, True),
        ("1", "/outra/lista", False, True),
        ("0", "/outra/lista", True, True),
        ("0", "/notas/lista", False, False),
        ("1", "/notas/lista", True, False),
    ],
)
async def test_meta_refresh_after_the_action_never_acts_twice(
    run_robot: RobotRunner,
    server: _Server,
    delay: str,
    target: str,
    navigation: bool,
    wait: bool,
) -> None:
    # A resposta do POST mostra a mesma tabela e, depois de ``delay`` segundos, leva a
    # página a ``target`` (que também mostra a tabela), na mesma volta ou já na seguinte.
    head = f"<meta http-equiv='refresh' content='{delay};url={target}'>"
    server.app = _forms_app(server, location=None, head=head)
    steps = [_click("cancelar", "Cancelar", row=True, navigation=navigation)]
    if wait:
        steps.append(_wait_url(target))
    manifest = _from_report(server.base, steps)
    outcome = await _run(run_robot, manifest)
    _assert_refused_once(outcome, server, "cancelar", irreversible=True)
    assert len([event for event in outcome.events if needs_ack(event)]) == 1

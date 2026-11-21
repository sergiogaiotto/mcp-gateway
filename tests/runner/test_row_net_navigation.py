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

import asyncio
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
from rapa.core.events import RunEvent, RunEventKind
from rapa.core.robot import Manifest
from rapa.runner.errors import ErrorCode
from rapa.runner.ports import ListEventSink, needs_ack
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


# -- uma navegação que a ação provoca tarde chega numa volta seguinte ----------------------


def _wait_text(text: str) -> dict[str, Any]:
    return {
        "kind": "wait_for",
        "id": "esperar",
        "title": "Esperar o aviso",
        "state": "visible",
        "timeout_ms": 6_000,
        "target": {
            "intent": f"aviso {text}",
            "locators": [{"by": "text", "value": text, "exact": True}],
        },
    }


def _late_app(server: _Server, how: str) -> _App:
    """O "Cancelar" da linha some com o aviso "Pronto" e leva a página, tarde, a outro
    endereço que mostra a mesma tabela (e o aviso): por um ``setTimeout`` depois do
    fetch que cancela, ou por um popup que cancela e depois leva a página que o abriu."""
    if how == "temporizador":
        go = (
            "fetch('/api/cancelar', {method: 'POST', body: n})"
            ".then(() => setTimeout(() => { location.href = '/notas/b'; }, 800));"
        )
    else:
        go = "window.open('/popup/cancelar?nota=' + n);"
    rows = "".join(
        f"<tr><td>{n}</td><td><button type='button' onclick=\"cancelar('{n}')\">Cancelar"
        "</button></td></tr>"
        for n in _NUMEROS
    )
    notas = _page(
        "<p id='pronto'>Pronto</p><button type='button'>Atualizar</button>"
        + _table(rows)
        + "<script>function cancelar(n) { document.getElementById('pronto').remove(); "
        + go
        + " }</script>"
    )
    popup = _page(
        "<p>Cancelada</p><script>setTimeout(() => "
        "{ window.opener.location.href = '/notas/lista'; }, 900);</script>"
    )

    def app(method: str, path: str, form: dict[str, list[str]]) -> tuple[int, dict[str, str], str]:
        parts = urlsplit(path)
        if parts.path == "/relatorio":
            return 200, {}, _RELATORIO
        if method == "POST" and parts.path == "/api/cancelar":
            server.writes.append(f"C{form['_corpo'][0]}")
            return 200, {}, "ok"
        if parts.path == "/popup/cancelar":
            server.writes.extend(f"C{n}" for n in parse_qs(parts.query).get("nota", []))
            return 200, {}, popup
        return 200, {}, notas

    return app


@pytest.mark.parametrize("how", ["temporizador", "popup"])
@pytest.mark.parametrize("first", ["atualizar_que_grava", "atualizar_que_le"])
async def test_late_navigation_of_the_action_in_the_next_iteration_never_acts_twice(
    run_robot: RobotRunner, server: _Server, how: str, first: str
) -> None:
    # A volta seguinte clica em "Atualizar" (um passo que pode mudar a página) antes de a
    # navegação provocada pela ação anterior começar; a página vai para outro endereço
    # com a mesma tabela, e o item repetido não age de novo nela.
    server.app = _late_app(server, how)
    effect = "write" if first == "atualizar_que_grava" else "read"
    steps = [
        _click("atualizar", "Atualizar", effect=effect),
        _wait_text("Pronto"),
        _click("cancelar", "Cancelar", row=True),
    ]
    outcome = await _run(run_robot, _from_report(server.base, steps))
    _assert_refused_once(outcome, server, "cancelar", irreversible=True)
    assert len(outcome.of(RunEventKind.STEP_SUCCEEDED, "atualizar")) == 2


async def test_late_navigation_in_an_inner_loop_never_acts_twice(
    run_robot: RobotRunner, server: _Server
) -> None:
    # O mesmo, numa repetição de dentro de outra.
    server.app = _late_app(server, "temporizador")
    manifest = _manifest(
        [
            _for_each(
                "inputs.pedidos",
                [
                    _navigate(server.base, "/relatorio", "abrir_relatorio"),
                    _read_notes(),
                    _navigate(server.base, "/notas", "abrir_notas"),
                    _for_each(
                        "vars.notas",
                        [
                            _click("atualizar", "Atualizar", effect="write"),
                            _wait_text("Pronto"),
                            _click("cancelar", "Cancelar", row=True),
                        ],
                        "cada_nota",
                    ),
                ],
                "cada_pedido",
            )
        ],
        inputs=[
            {
                "name": "pedidos",
                "title": "Pedidos",
                "type": "list",
                "item_fields": [{"name": "pedido", "title": "Pedido", "type": "string"}],
            }
        ],
    )
    outcome = await _run(run_robot, manifest, inputs={"pedidos": [{"pedido": "1"}]})
    _assert_refused_once(outcome, server, "cancelar", irreversible=True)


async def test_a_page_the_robot_navigates_to_in_the_next_iteration_is_another_page(
    run_robot: RobotRunner, server: _Server
) -> None:
    # A nota 1 de cada filial. A volta da filial "a" cancela e o POST redireciona (303)
    # de volta para a filial; a volta da filial "b" abre a página dela com um
    # ``navigate``, que redireciona (302) para a lista de notas dela. As navegações do
    # próprio ``navigate`` não contam para a volta anterior, e as duas notas são
    # canceladas, cada uma uma vez.
    def filial(name: str) -> str:
        rows = (
            f"<tr><td>1</td><td><form method='post' action='/filial/{name}/cancelar'>"
            f"<input type='hidden' name='nota' value='{name}1'><button>Cancelar</button>"
            "</form></td></tr>"
        )
        return _page(f"<h1>Filial {name}</h1>" + _table(rows))

    def app(method: str, path: str, form: dict[str, list[str]]) -> tuple[int, dict[str, str], str]:
        where = urlsplit(path).path
        if method == "POST":
            server.writes.extend(form.get("nota", []))
            return 303, {"location": where.removesuffix("/cancelar") + "/notas"}, ""
        if where in ("/filial/a", "/filial/b"):
            return 302, {"location": f"{where}/notas"}, ""
        return 200, {}, filial(where.split("/")[2])

    server.app = app
    manifest = _manifest(
        [
            _for_each(
                "inputs.filiais",
                [
                    _navigate(server.base, "/filial/{{ item.filial }}", "abrir_filial"),
                    {
                        **_click("cancelar", "Cancelar", navigation=True),
                        "target": {
                            "intent": "botão Cancelar da nota 1",
                            "locators": [
                                {"by": "role", "value": "button", "name": "Cancelar", "exact": True}
                            ],
                            "within": {"by": "role", "value": "row"},
                            "within_has_text": "1",
                        },
                    },
                ],
                "cada_filial",
            )
        ],
        inputs=[
            {
                "name": "filiais",
                "title": "Filiais",
                "type": "list",
                "item_fields": [{"name": "filial", "title": "Filial", "type": "string"}],
            }
        ],
    )
    outcome = await _run(
        run_robot, manifest, inputs={"filiais": [{"filial": "a"}, {"filial": "b"}]}
    )
    assert outcome.result.state is RunState.SUCCEEDED, outcome.result.error_message
    assert server.writes == ["a1", "b1"]


# -- a página muda entre a conferência da linha e a ação ------------------------------------


def _sides_app(server: _Server, *, enabled_after_ms: int = 0) -> _App:
    """A nota 120 aparece no relatório duas vezes, uma em cada lado ("a" e "c", páginas
    diferentes). O "Cancelar" da linha abre um popup que cancela a nota; o popup do lado
    "a", 2 s depois, leva a página que o abriu de volta a /notas/a. Em /notas/c, o botão
    pode ficar desabilitado por ``enabled_after_ms`` (o clique espera por ele)."""
    relatorio = _page(
        "<table aria-label='Notas'><thead><tr><th>numero</th><th>lado</th></tr></thead><tbody>"
        "<tr><td>120</td><td>a</td></tr><tr><td>120</td><td>c</td></tr></tbody></table>"
    )

    def notas(lado: str, disabled_ms: int) -> str:
        script = (
            "<script>setTimeout(() => { document.getElementById('cancelar').disabled = false; "
            f"}}, {disabled_ms});</script>"
            if disabled_ms
            else ""
        )
        rows = (
            f"<tr><td>120</td><td><button id='cancelar' type='button'"
            f"{' disabled' if disabled_ms else ''} "
            f"onclick=\"window.open('/popup/cancelar?nota=120&lado={lado}')\">Cancelar</button>"
            "</td></tr><tr><td>7</td><td><button type='button'>Cancelar</button></td></tr>"
        )
        return _page(f"<h1>Lado {lado}</h1>" + _table(rows) + script)

    def app(method: str, path: str, form: dict[str, list[str]]) -> tuple[int, dict[str, str], str]:
        parts = urlsplit(path)
        if parts.path == "/relatorio":
            return 200, {}, relatorio
        if parts.path == "/popup/cancelar":
            lado = parse_qs(parts.query).get("lado", ["?"])[0]
            server.writes.append(f"C120@{lado}")
            back = (
                "<script>setTimeout(() => { window.opener.location.href = '/notas/a'; }, 2000);"
                "</script>"
                if lado == "a"
                else ""
            )
            return 200, {}, _page("<p>Cancelada</p>" + back)
        if parts.path in ("/notas/a", "/notas/c"):
            lado = parts.path.rsplit("/", 1)[1]
            return 200, {}, notas(lado, enabled_after_ms if lado == "c" else 0)
        return 404, {}, "nada"

    return app


def _sides_manifest(base: str, effect: str, timeout_ms: int) -> Manifest:
    read = _read_notes() | {"columns": ["numero", "lado"]}
    cancel = _click("cancelar", "Cancelar", row=True, effect=effect) | {"timeout_ms": timeout_ms}
    return _manifest(
        [
            _navigate(base, "/relatorio", "abrir_relatorio"),
            read,
            _for_each(
                "vars.notas",
                [_navigate(base, "/notas/{{ item.lado }}", "abrir_lado"), cancel],
                "cada_nota",
            ),
        ]
    )


class _SlowSecondWarning(ListEventSink):
    """Guarda os eventos; só a confirmação do segundo aviso de passo irreversível demora."""

    def __init__(self, delay_s: float) -> None:
        super().__init__()
        self.delay_s = delay_s
        self.warnings = 0

    async def emit(self, event: RunEvent) -> None:
        await super().emit(event)
        if needs_ack(event):
            self.warnings += 1
            if self.warnings == 2:
                await asyncio.sleep(self.delay_s)


@pytest.mark.parametrize("timeout_ms", [1_000, 15_000])
async def test_a_page_that_moves_during_the_irreversible_warning_is_checked_again(
    run_robot: RobotRunner, server: _Server, timeout_ms: int
) -> None:
    # A volta 1 abre /notas/c com um ``navigate`` (outra página, por desenho) e confere a
    # linha lá. A confirmação do aviso demora 4 s e, nesse meio tempo, o popup da volta 0
    # leva a página de volta a /notas/a, onde a nota 120 já foi cancelada. Depois da
    # confirmação, a página e a linha são conferidas de novo antes de agir.
    server.app = _sides_app(server)
    events = _SlowSecondWarning(delay_s=4.0)
    outcome = await _run(
        run_robot, _sides_manifest(server.base, "irreversible", timeout_ms), events=events
    )
    result = outcome.result
    assert server.writes == ["C120@a"], result.error_message  # nunca duas vezes
    assert result.state is RunState.INDETERMINATE  # a volta 0 já tinha agido
    assert result.error_code == ErrorCode.ELEMENTO_AMBIGUO, result.error_message
    assert result.failed_step_id == "cancelar"
    assert "A página mudou" in (result.error_message or "")
    assert events.warnings == 2  # o segundo aviso foi dado, mas nada foi feito depois dele
    assert len(outcome.of(RunEventKind.STEP_SUCCEEDED, "cancelar")) == 1
    assert_seq(outcome.events)


@pytest.mark.parametrize("effect", ["write", "irreversible"])
async def test_a_click_waiting_for_the_button_never_lands_on_another_page(
    run_robot: RobotRunner, server: _Server, effect: str
) -> None:
    # Sem confirmação lenta: em /notas/c, o botão fica desabilitado por 6 s e o clique
    # espera por ele; nesse meio tempo, o popup da volta 0 leva a página de volta a
    # /notas/a. O clique vai para o botão conferido em /notas/c, que deixou de existir, e
    # nunca para o da mesma linha em /notas/a.
    server.app = _sides_app(server, enabled_after_ms=6_000)
    outcome = await _run(run_robot, _sides_manifest(server.base, effect, 10_000))
    result = outcome.result
    assert server.writes == ["C120@a"], result.error_message  # nunca duas vezes
    expected = RunState.INDETERMINATE if effect == "irreversible" else RunState.FAILED
    assert result.state is expected, result.error_message
    assert result.failed_step_id == "cancelar"
    assert result.error_code in (ErrorCode.ACAO_FALHOU, ErrorCode.ELEMENTO_AMBIGUO)
    assert len(outcome.of(RunEventKind.STEP_SUCCEEDED, "cancelar")) == 1
    assert_seq(outcome.events)


# -- a página muda enquanto o clique espera o botão, ou na hora do clique -------------------


def _spa_sides_app(server: _Server, *, disabled_ms: int) -> _App:
    """Como em :func:`_sides_app`, mas /notas/a e /notas/c são a mesma aplicação de página
    única: o botão lê o lado do endereço na hora do clique, e o popup da volta 0, 1,5 s
    depois, leva a página que o abriu a /notas/a por ``pushState`` (o mesmo documento e os
    mesmos nós, como numa renderização com chaves). Em /notas/c, o botão fica desabilitado
    por ``disabled_ms``."""
    relatorio = _page(
        "<table aria-label='Notas'><thead><tr><th>numero</th><th>lado</th></tr></thead><tbody>"
        "<tr><td>120</td><td>a</td></tr><tr><td>120</td><td>c</td></tr></tbody></table>"
    )
    script = (
        "<script>const lado = location.pathname.split('/').pop();"
        "if (lado === 'c') { const botao = document.getElementById('cancelar');"
        f" botao.disabled = true; setTimeout(() => {{ botao.disabled = false; }}, {disabled_ms}); }}"
        "</script>"
    )
    rows = (
        "<tr><td>120</td><td><button id='cancelar' type='button' onclick=\"window.open("
        "'/popup/cancelar?nota=120&lado=' + location.pathname.split('/').pop())\">Cancelar"
        "</button></td></tr><tr><td>7</td><td><button type='button'>Cancelar</button></td></tr>"
    )
    notas = _page(_table(rows) + script)

    def app(method: str, path: str, form: dict[str, list[str]]) -> tuple[int, dict[str, str], str]:
        parts = urlsplit(path)
        if parts.path == "/relatorio":
            return 200, {}, relatorio
        if parts.path == "/popup/cancelar":
            lado = parse_qs(parts.query).get("lado", ["?"])[0]
            server.writes.append(f"C120@{lado}")
            back = (
                "<script>setTimeout(() => { window.opener.history.pushState({}, '', '/notas/a'); "
                "}, 1500);</script>"
                if lado == "a"
                else ""
            )
            return 200, {}, _page("<p>Cancelada</p>" + back)
        if parts.path in ("/notas/a", "/notas/c"):
            return 200, {}, notas
        return 404, {}, "nada"

    return app


@pytest.mark.parametrize("effect", ["write", "irreversible"])
async def test_a_click_waiting_for_the_button_never_acts_after_a_same_document_move(
    run_robot: RobotRunner, server: _Server, effect: str
) -> None:
    # Em /notas/c, o botão fica desabilitado por 4 s; nesse meio tempo, o popup da volta 0
    # muda o endereço da página para /notas/a sem trocar o documento nem o botão, que
    # cancelaria de novo a nota 120 de /notas/a. A espera pelo botão vem antes da
    # conferência da hora de agir, que vê a mudança e não age.
    server.app = _spa_sides_app(server, disabled_ms=4_000)
    outcome = await _run(run_robot, _sides_manifest(server.base, effect, 10_000))
    result = outcome.result
    assert server.writes == ["C120@a"], result.error_message  # nunca duas vezes
    expected = RunState.INDETERMINATE if effect == "irreversible" else RunState.FAILED
    assert result.state is expected, result.error_message
    assert result.error_code == ErrorCode.ELEMENTO_AMBIGUO, result.error_message
    assert result.failed_step_id == "cancelar"
    assert "A página mudou" in (result.error_message or "")
    assert len(outcome.of(RunEventKind.STEP_SUCCEEDED, "cancelar")) == 1
    assert_seq(outcome.events)


def _api_app(server: _Server, html: str) -> _App:
    """Uma página só, em /notas; cada POST para /api é anotado com o corpo dele."""

    def app(method: str, path: str, form: dict[str, list[str]]) -> tuple[int, dict[str, str], str]:
        route = urlsplit(path).path
        if route == "/notas":
            return 200, {}, html
        if route == "/api" and method == "POST":
            server.writes.append(form["_corpo"][0])
            return 200, {}, "ok"
        return 404, {}, "nada"

    return app


def _cancel_120(base: str) -> Manifest:
    cancel = _click("cancelar", "Cancelar", effect="write") | {"timeout_ms": 8_000}
    cancel["target"] |= {"within": {"by": "role", "value": "row"}, "within_has_text": "120"}
    return _manifest([_navigate(base, "/notas", "abrir"), cancel])


# O "Cancelar" cancela a nota da linha em que está na hora do clique.
_CANCELA_A_LINHA = (
    "<button type='button' disabled onclick=\"fetch('/api', {method: 'POST', "
    "body: 'C' + this.closest('tr').cells[0].textContent})\">Cancelar</button>"
)
# Enquanto os botões estão desabilitados, a página troca os números das linhas sem trocar os
# nós (uma lista virtual que reaproveita as linhas).
_LINHAS_TROCADAS = _page(
    _table(
        f"<tr><td>120</td><td>{_CANCELA_A_LINHA}</td></tr>"
        f"<tr><td>7</td><td>{_CANCELA_A_LINHA}</td></tr>"
    )
    + "<script>setTimeout(() => { const cells = [...document.querySelectorAll('tbody tr')]"
    ".map((row) => row.cells[0]); [cells[0].textContent, cells[1].textContent] = "
    "[cells[1].textContent, cells[0].textContent]; }, 1500);"
    "setTimeout(() => { document.querySelectorAll('button').forEach((b) => { "
    "b.disabled = false; }); }, 3000);</script>"
)
# A linha 120 troca o botão desabilitado por outro nó (o mesmo botão montado de novo), que
# depois fica habilitado.
_BOTAO_MONTADO_DE_NOVO = _page(
    _table(
        "<tr><td>120</td><td id='celula'><button type='button' disabled>Cancelar</button></td>"
        "</tr><tr><td>7</td><td><button type='button'>Cancelar</button></td></tr>"
    )
    + "<script>setTimeout(() => { document.getElementById('celula').innerHTML = "
    "\"<button type='button' disabled onclick=\\\"fetch('/api', {method: 'POST', "
    "body: 'C120'})\\\">Cancelar</button>\"; }, 1000);"
    "setTimeout(() => { document.querySelector('#celula button').disabled = false; }, 2000);"
    "</script>"
)


async def test_a_row_reused_in_place_while_the_click_waits_is_never_clicked(
    run_robot: RobotRunner, server: _Server
) -> None:
    # O botão conferido continua na página, mas a linha dele passou a ser a da nota 7.
    # Depois da espera pelo botão, a linha é conferida de novo: nada é cancelado.
    server.app = _api_app(server, _LINHAS_TROCADAS)
    outcome = await _run(run_robot, _cancel_120(server.base))
    result = outcome.result
    assert server.writes == [], result.error_message  # nunca a nota 7
    assert result.state is RunState.FAILED
    assert result.error_code == ErrorCode.ELEMENTO_AMBIGUO, result.error_message
    assert result.failed_step_id == "cancelar"
    assert_seq(outcome.events)


async def test_a_button_mounted_again_in_the_same_row_is_checked_again_and_clicked(
    run_robot: RobotRunner, server: _Server
) -> None:
    # O botão conferido saiu da página antes de qualquer clique, sem navegação: o alvo, a
    # página, a linha e a rede são conferidos de novo, uma vez, e o clique vai para o botão
    # novo da mesma linha.
    server.app = _api_app(server, _BOTAO_MONTADO_DE_NOVO)
    outcome = await _run(run_robot, _cancel_120(server.base))
    result = outcome.result
    assert result.state is RunState.SUCCEEDED, result.error_message
    assert server.writes == ["C120"]
    assert_seq(outcome.events)


# Um ouvinte da página, na janela (depois do vigia do executor, que é posto antes dos
# scripts dela), muda a página no ``pointerdown`` do clique: o endereço (``pushState``) ou
# os números das linhas.
_NO_POINTERDOWN = {
    "endereco": "history.pushState({}, '', '/notas/outra');",
    "linha": "const cells = [...document.querySelectorAll('tbody tr')].map((row) => row.cells[0]);"
    " [cells[0].textContent, cells[1].textContent] = [cells[1].textContent, cells[0].textContent];",
}


@pytest.mark.parametrize("where", ["janela", "botao"])
@pytest.mark.parametrize("change", list(_NO_POINTERDOWN))
async def test_a_page_that_changes_in_the_moment_of_the_click_is_never_clicked(
    run_robot: RobotRunner, server: _Server, change: str, where: str
) -> None:
    # Na hora do clique, depois de toda a conferência: o vigia deixa passar o primeiro
    # evento, a página muda com ele, e o vigia vê o evento seguinte do clique já com outro
    # endereço, ou com a linha do botão mostrando outra nota, e interrompe o clique antes do
    # ``click``. Nada é cancelado no portal, e o passo diz que o clique chegou à página.
    button = _CANCELA_A_LINHA.replace(" disabled", "")
    html = _page(
        _table(f"<tr><td>120</td><td>{button}</td></tr><tr><td>7</td><td>{button}</td></tr>")
        + "<script>let feito = false; const muda = () => {"
        f" if (feito) return; feito = true; {_NO_POINTERDOWN[change]} }};"
        + (
            " window.addEventListener('pointerdown', muda, true);"
            if where == "janela"
            else " document.querySelectorAll('tbody button')"
            ".forEach((b) => b.addEventListener('pointerdown', muda));"
        )
        + "</script>"
    )
    server.app = _api_app(server, html)
    outcome = await _run(run_robot, _cancel_120(server.base))
    result = outcome.result
    assert server.writes == [], result.error_message
    assert result.state is RunState.FAILED
    assert result.error_code == ErrorCode.ELEMENTO_AMBIGUO, result.error_message
    assert result.failed_step_id == "cancelar"
    assert "A página mudou durante o clique" in (result.error_message or "")
    assert_seq(outcome.events)

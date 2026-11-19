"""O anúncio de um passo que grava e a confirmação do plano de controle.

Só os passos simples que agem se anunciam como gravação (``grava``, o aviso de passo
irreversível) e esperam a confirmação; um contêiner nunca, e o efeito dele vale para
os passos de dentro. A espera pela confirmação não consome o prazo do passo, e uma
confirmação que não vem é a falha do próprio passo, mesmo dentro de um contêiner.

As páginas são servidas pelo próprio navegador (``route.fulfill``).
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import BrowserContext, Route

from rapa.control.services.queue import _marks_write
from rapa.core import policy
from rapa.core.contracts import RunState
from rapa.core.events import RunEvent, RunEventKind
from rapa.core.robot import Manifest
from rapa.runner.errors import ErrorCode
from rapa.runner.ports import AckingEventSink, ListEventSink, needs_ack, waits_for_ack
from tests.runner.support import ContextSetup, RobotRunner, assert_seq

pytestmark = pytest.mark.browser

HOST = "paginas.rapa.test"
BASE = f"http://{HOST}"

# Cada clique em "Gravar" fica no registro, que sobrevive à navegação.
_PAGINA = """<!doctype html><meta charset="utf-8"><title>Notas</title>
<p id="log"></p><script>document.getElementById('log').textContent = localStorage.log || ''</script>
<button onclick="localStorage.log = (localStorage.log || '') + 'G;';
document.getElementById('log').textContent = localStorage.log">Gravar</button>"""


def _serve() -> ContextSetup:
    async def prepare(context: BrowserContext) -> None:
        async def handler(route: Route) -> None:
            await route.fulfill(content_type="text/html; charset=utf-8", body=_PAGINA)

        await context.route("**/*", handler)

    return prepare


class _Log:
    """O registro dos cliques, lido da página depois da execução."""

    def __init__(self) -> None:
        self.log: str | None = None

    async def __call__(self, context: BrowserContext) -> None:
        if context.pages:
            self.log = await context.pages[0].evaluate("() => localStorage.log || ''")


class _SlowAcks(ListEventSink):
    """Guarda os eventos e só "confirma" os que esperam confirmação depois de ``delay_s``."""

    def __init__(self, delay_s: float) -> None:
        super().__init__()
        self.delay_s = delay_s

    async def emit(self, event: RunEvent) -> None:
        await super().emit(event)
        if waits_for_ack(event):
            await asyncio.sleep(self.delay_s)


def _manifest(steps: list[dict[str, Any]], **extra: Any) -> Manifest:
    manifest = Manifest.model_validate(
        {
            "name": "robo_de_teste",
            "title": "Robô de teste",
            "description": "Robô montado pelos testes do anúncio de gravação.",
            "allowed_domains": [HOST],
            "steps": steps,
            **extra,
        }
    )
    assert policy.check(manifest) == []
    return manifest


def _open() -> dict[str, Any]:
    return {"kind": "navigate", "id": "abrir", "title": "Abrir a página", "url": f"{BASE}/notas"}


def _gravar(
    effect: str = "write", *, step_id: str = "gravar", name: str = "Gravar"
) -> dict[str, Any]:
    return {
        "kind": "click",
        "id": step_id,
        "title": "Gravar",
        "effect": effect,
        "timeout_ms": 1_000,
        "target": {
            "intent": "botão Gravar",
            "locators": [{"by": "role", "value": "button", "name": name, "exact": True}],
        },
    }


def _ler() -> dict[str, Any]:
    return {
        "kind": "extract_text",
        "id": "ler",
        "title": "Ler o registro",
        "timeout_ms": 1_000,
        "target": {"intent": "registro", "locators": [{"by": "css", "value": "#log"}]},
        "into": "registro",
    }


def _for_each(steps: list[dict[str, Any]], effect: str = "read") -> dict[str, Any]:
    return {
        "kind": "for_each",
        "id": "cada",
        "title": "Para cada nota",
        "effect": effect,
        "items": "inputs.notas",
        "steps": steps,
    }


def _if(steps: list[dict[str, Any]], effect: str = "read") -> dict[str, Any]:
    return {
        "kind": "if",
        "id": "se",
        "title": "Se é para gravar",
        "effect": effect,
        "condition": {"op": "equals", "left": "{{ inputs.gravar }}", "right": "sim"},
        "then": steps,
    }


_NOTAS = {
    "name": "notas",
    "title": "Notas",
    "type": "list",
    "item_fields": [{"name": "numero", "title": "Número", "type": "string"}],
}
_GRAVAR = {"name": "gravar", "title": "Gravar", "type": "string"}


def _started(events: list[RunEvent], step_id: str) -> list[RunEvent]:
    return [e for e in events if e.kind is RunEventKind.STEP_STARTED and e.step_id == step_id]


# -- a espera pela confirmação não consome o prazo do passo --------------------------------


@pytest.mark.parametrize("effect", ["write", "irreversible"])
async def test_waiting_for_the_ack_does_not_use_up_the_step_timeout(
    run_robot: RobotRunner, effect: str
) -> None:
    # A confirmação demora mais que o prazo do passo (1 s): o passo ainda tem o prazo
    # inteiro depois dela, e grava uma vez.
    sink = _SlowAcks(delay_s=1.5)
    log = _Log()
    outcome = await run_robot(
        _manifest([_open(), _gravar(effect)]), events=sink, prepare_context=_serve(), after_run=log
    )
    result = outcome.result
    assert result.state is RunState.SUCCEEDED, result.error_message
    assert log.log == "G;"
    [announced] = [e for e in _started(sink.events, "gravar") if waits_for_ack(e)]
    assert announced.data["efeito"] == effect


# -- a confirmação que não vem é a falha do próprio passo ----------------------------------


@pytest.mark.parametrize("effect", ["write", "irreversible"])
@pytest.mark.parametrize("where", ["fora", "repeticao", "se"])
async def test_missing_ack_fails_the_step_that_waited_for_it(
    run_robot: RobotRunner, tmp_path: Path, where: str, effect: str
) -> None:
    inner = ListEventSink()
    sink = AckingEventSink(inner, tmp_path / "sem-confirmacao", timeout_s=0.3)
    if where == "fora":
        steps, inputs = [_open(), _gravar(effect)], {}
        manifest = _manifest(steps)
    elif where == "repeticao":
        manifest = _manifest([_open(), _for_each([_gravar(effect)])], inputs=[_NOTAS])
        inputs = {"notas": [{"numero": "1"}]}
    else:
        manifest = _manifest([_open(), _if([_gravar(effect)])], inputs=[_GRAVAR])
        inputs = {"gravar": "sim"}
    log = _Log()
    outcome = await run_robot(
        manifest, inputs, events=sink, prepare_context=_serve(), after_run=log
    )
    result = outcome.result
    events = inner.events
    assert log.log == ""  # o passo não agiu
    assert result.state is RunState.FAILED, result.error_message
    assert result.error_code == ErrorCode.SEM_CONFIRMACAO
    assert result.failed_step_id == "gravar"
    assert not result.irreversible_started
    # Um anúncio só (nunca repetido sem a marca), e a falha do próprio passo.
    [announced] = _started(events, "gravar")
    assert waits_for_ack(announced)
    [failure] = [e for e in events if e.kind is RunEventKind.STEP_FAILED and e.step_id == "gravar"]
    assert failure.data["codigo"] == ErrorCode.SEM_CONFIRMACAO
    if where != "fora":
        container = "cada" if where == "repeticao" else "se"
        [outer] = [
            e for e in events if e.kind is RunEventKind.STEP_FAILED and e.step_id == container
        ]
        assert outer.data["passo_interno"] == "gravar"
    assert_seq(events)


# -- só os passos simples se anunciam como gravação ----------------------------------------


@pytest.mark.parametrize("container", ["repeticao", "se"])
async def test_containers_never_announce_a_write_and_pass_their_effect_down(
    run_robot: RobotRunner, container: str
) -> None:
    # O contêiner gravaria (efeito declarado "write"), mas quem age são os passos de
    # dentro: a leitura de dentro se anuncia como gravação (o efeito só sobe), e o
    # contêiner, não. Se o executor parasse antes do primeiro passo de dentro, nada
    # teria sido gravado, e a execução poderia voltar para a fila.
    body = [_ler(), _gravar("read")]
    if container == "repeticao":
        manifest = _manifest([_open(), _for_each(body, "write")], inputs=[_NOTAS])
        inputs: dict[str, Any] = {"notas": [{"numero": "1"}]}
        container_id = "cada"
    else:
        manifest = _manifest([_open(), _if(body, "write")], inputs=[_GRAVAR])
        inputs = {"gravar": "sim"}
        container_id = "se"
    log = _Log()
    outcome = await run_robot(manifest, inputs, prepare_context=_serve(), after_run=log)
    assert outcome.result.state is RunState.SUCCEEDED, outcome.result.error_message
    assert log.log == "G;"
    [block] = outcome.of(RunEventKind.STEP_STARTED, container_id)
    assert block.data["efeito_do_bloco"] == "write"
    assert "efeito" not in block.data and "grava" not in block.data
    assert not waits_for_ack(block) and not _marks_write(block)
    for step_id in ("ler", "gravar"):
        [started] = outcome.of(RunEventKind.STEP_STARTED, step_id)
        assert started.data["efeito"] == "write"
        assert started.data["grava"] is True
        assert waits_for_ack(started) and _marks_write(started)
    # A primeira marca de gravação é a do primeiro passo de dentro.
    first = next(e for e in outcome.events if _marks_write(e))
    assert first.step_id == "ler"


async def test_inner_step_of_a_writing_block_waits_for_the_ack(
    run_robot: RobotRunner, tmp_path: Path
) -> None:
    # O clique de dentro foi declarado como leitura, mas o bloco grava: sem a
    # confirmação, ele nunca age.
    inner = ListEventSink()
    sink = AckingEventSink(inner, tmp_path / "sem-confirmacao", timeout_s=0.3)
    manifest = _manifest([_open(), _for_each([_gravar("read")], "write")], inputs=[_NOTAS])
    log = _Log()
    outcome = await run_robot(
        manifest,
        {"notas": [{"numero": "1"}]},
        events=sink,
        prepare_context=_serve(),
        after_run=log,
    )
    assert log.log == ""
    assert outcome.result.error_code == ErrorCode.SEM_CONFIRMACAO
    assert outcome.result.failed_step_id == "gravar"
    [block] = _started(inner.events, "cada")
    assert not waits_for_ack(block)


async def test_irreversible_block_warns_before_each_inner_step_not_before_the_block(
    run_robot: RobotRunner,
) -> None:
    # O primeiro passo de dentro não encontra o botão: nada começou, e a execução
    # falha (não fica indeterminada), sem aviso de passo irreversível.
    manifest = _manifest(
        [_open(), _for_each([_gravar("read", name="Outro botão")], "irreversible")],
        inputs=[_NOTAS],
    )
    outcome = await run_robot(manifest, {"notas": [{"numero": "1"}]}, prepare_context=_serve())
    result = outcome.result
    assert result.state is RunState.FAILED, result.error_message
    assert result.error_code == ErrorCode.ELEMENTO_NAO_ENCONTRADO
    assert not result.irreversible_started
    assert not [event for event in outcome.events if needs_ack(event)]
    # Com o botão certo, o passo de dentro avisa antes de agir, e o bloco nunca.
    manifest = _manifest(
        [_open(), _for_each([_ler(), _gravar("read")], "irreversible")], inputs=[_NOTAS]
    )
    log = _Log()
    outcome = await run_robot(
        manifest, {"notas": [{"numero": "1"}]}, prepare_context=_serve(), after_run=log
    )
    assert outcome.result.state is RunState.SUCCEEDED, outcome.result.error_message
    assert log.log == "G;"
    warnings = [event for event in outcome.events if needs_ack(event)]
    assert [event.step_id for event in warnings] == ["ler", "gravar"]
    assert outcome.result.irreversible_started

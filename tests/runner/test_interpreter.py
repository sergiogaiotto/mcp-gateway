"""Testes do interpretador com o Chromium e o portal de teste."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Browser, BrowserContext, Route

from rapa.core.contracts import RunMode, RunState
from rapa.core.events import RunEvent, RunEventKind
from rapa.core.ids import new_id
from rapa.core.robot import Manifest
from rapa.fixtures.portal import DEFAULT_USER
from rapa.fixtures.robots import baixar_notas, emitir_protocolo, login_steps, portal_credentials
from rapa.fixtures.server import RunningPortal
from rapa.runner.browser import run_context
from rapa.runner.errors import ErrorCode
from rapa.runner.interpreter import MASK, Interpreter
from rapa.runner.ports import (
    DictSecretProvider,
    DirectoryArtifactSink,
    ListEventSink,
    LocalFileProvider,
)
from rapa.runner.result import ArtifactKind
from tests.runner.support import (
    PROTOCOLO_INPUTS,
    SECRETS,
    RobotRunner,
    assert_no_secrets,
    assert_seq,
    edit,
    file_bytes,
    find_step,
    portal_download,
    set_step,
)

pytestmark = pytest.mark.browser


def _target(intent: str, *locators: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"intent": intent, "locators": list(locators), **extra}


def _manifest(portal: RunningPortal, steps: list[dict[str, Any]], **extra: Any) -> Manifest:
    """Robô de teste sobre o portal, com login opcional nos passos."""
    return Manifest.model_validate(
        {
            "name": "robo_de_teste",
            "title": "Robô de teste",
            "description": "Robô montado pelos testes do executor.",
            "allowed_domains": [portal.host],
            "credentials": portal_credentials(portal.base_url),
            "steps": steps,
            **extra,
        }
    )


# -- robôs de exemplo ------------------------------------------------------------


async def test_emitir_protocolo_issues_exactly_one_protocol(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    manifest = emitir_protocolo(portal_v1.base_url, portal_v1.host)
    outcome = await run_robot(manifest, PROTOCOLO_INPUTS)
    result = outcome.result

    assert result.state is RunState.SUCCEEDED, result.error_message
    assert result.error_code is None
    assert len(portal_v1.state.protocolos) == 1
    issued = portal_v1.state.protocolos[0]
    assert result.outputs == {"protocolo": issued["numero"]}
    assert issued["cpf"] == PROTOCOLO_INPUTS["cpf"]  # máscara digitada tecla a tecla
    assert result.irreversible_started
    assert result.steps_done == len(manifest.steps)

    assert_seq(outcome.events)
    started = outcome.of(RunEventKind.STEP_STARTED, "emitir")
    assert len(started) == 1 and started[0].data["irreversivel"] is True
    # Tela depois do passo irreversível, registrada como artefato.
    shots = [a for a in result.artifacts if a.kind == ArtifactKind.SCREENSHOT]
    assert [a.name for a in shots] == ["tela-001-emitir-depois.png"]
    assert hashlib.sha256(file_bytes(shots[0].path)).hexdigest() == shots[0].sha256
    assert outcome.of(RunEventKind.ARTIFACT, "emitir")
    assert_no_secrets(outcome.dump())


async def test_until_first_write_stops_before_issuing(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    manifest = emitir_protocolo(portal_v1.base_url, portal_v1.host)
    outcome = await run_robot(manifest, PROTOCOLO_INPUTS, mode=RunMode.UNTIL_FIRST_WRITE)
    result = outcome.result

    assert result.state is RunState.SUCCEEDED
    assert result.error_code == ErrorCode.PAROU_ANTES_DE_GRAVAR
    assert not result.irreversible_started
    assert portal_v1.state.protocolos == []
    stops = outcome.of(RunEventKind.RUN_STOPPED_BEFORE_WRITE)
    assert [e.step_id for e in stops] == ["emitir"]
    assert not outcome.of(RunEventKind.STEP_STARTED, "emitir")
    assert outcome.kinds()[-2:] == [
        RunEventKind.RUN_STOPPED_BEFORE_WRITE,
        RunEventKind.RUN_SUCCEEDED,
    ]
    assert result.outputs == {"protocolo": None}


async def test_baixar_notas_downloads_ten_files(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    outcome = await run_robot(baixar_notas(portal_v1.base_url, portal_v1.host))
    result = outcome.result

    assert result.state is RunState.SUCCEEDED, result.error_message
    numbers = [nota.numero for nota in portal_v1.state.notas[:10]]
    assert portal_v1.state.downloads == numbers
    downloads = [a for a in result.artifacts if a.kind == ArtifactKind.DOWNLOAD]
    assert [a.name for a in downloads] == [f"nota-{n}.pdf" for n in numbers]
    for ref, numero in zip(downloads, numbers, strict=True):
        content = file_bytes(ref.path)
        assert ref.size == len(content)
        assert ref.sha256 == hashlib.sha256(content).hexdigest()
        assert content == portal_download(portal_v1, numero)

    assert result.outputs is not None
    notas = result.outputs["notas"]
    assert [row["numero"] for row in notas] == numbers
    assert notas[0] == {
        "numero": "001000",
        "cliente": "Padaria Pão Quente Ltda",
        "valor": "123,40",
        "situacao": "Autorizada",
        "arquivo": "Baixar",
    }
    # Só leitura: nenhuma tela além das de falha.
    assert not [a for a in result.artifacts if a.kind == ArtifactKind.SCREENSHOT]
    iterations = [
        e.data.get("iteracao") for e in outcome.of(RunEventKind.STEP_STARTED, "baixar_nota")
    ]
    assert iterations == list(range(10))
    assert_seq(outcome.events)
    assert_no_secrets(outcome.dump())


async def test_portal_v2_fails_at_login_button(
    run_robot: RobotRunner, portal_v2: RunningPortal
) -> None:
    manifest = set_step(
        emitir_protocolo(portal_v2.base_url, portal_v2.host), "entrar", timeout_ms=2_000
    )
    outcome = await run_robot(manifest, PROTOCOLO_INPUTS)
    result = outcome.result

    assert result.state is RunState.FAILED
    assert result.error_code == ErrorCode.ELEMENTO_NAO_ENCONTRADO
    assert result.failed_step_id == "entrar"
    assert not result.irreversible_started
    assert portal_v2.state.protocolos == []
    # O campo de usuário mudou de rótulo; o passo de leitura usou o CSS alternativo.
    healed = outcome.of(RunEventKind.STEP_HEALED, "digitar_usuario")
    assert len(healed) == 1 and healed[0].data["localizador"] == 1
    failed = outcome.of(RunEventKind.STEP_FAILED, "entrar")
    assert failed[0].data["codigo"] == "elemento_nao_encontrado"
    assert failed[0].data["contagens"] == [0]
    assert "botão Entrar" in failed[0].data["mensagem"]
    shots = [a for a in result.artifacts if a.kind == ArtifactKind.SCREENSHOT]
    assert [a.name for a in shots] == ["tela-001-entrar-falha.png"]
    assert_seq(outcome.events)
    assert_no_secrets(outcome.dump())


# -- localizadores ------------------------------------------------------------------


async def test_ambiguous_locator_fails(run_robot: RobotRunner, portal_v1: RunningPortal) -> None:
    manifest = _manifest(
        portal_v1,
        [
            {
                "kind": "navigate",
                "id": "abrir",
                "title": "Abrir",
                "url": f"{portal_v1.base_url}/login",
            },
            {
                "kind": "extract_text",
                "id": "ler_campo",
                "title": "Ler um campo",
                "timeout_ms": 1_000,
                "target": _target("campo", {"by": "css", "value": "input"}),
                "into": "campo",
            },
        ],
    )
    outcome = await run_robot(manifest)
    assert outcome.result.state is RunState.FAILED
    assert outcome.result.error_code == ErrorCode.ELEMENTO_AMBIGUO
    assert outcome.result.failed_step_id == "ler_campo"
    assert "2 elementos" in (outcome.result.error_message or "")


async def test_read_step_heals_past_ambiguous_locator(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    manifest = _manifest(
        portal_v1,
        [
            {
                "kind": "navigate",
                "id": "abrir",
                "title": "Abrir",
                "url": f"{portal_v1.base_url}/login",
            },
            {
                "kind": "fill",
                "id": "digitar",
                "title": "Digitar o usuário",
                "target": _target(
                    "campo Usuário",
                    {"by": "css", "value": "input"},
                    {"by": "label", "value": "Usuário"},
                ),
                "value": {"source": "text", "template": "fulano"},
            },
        ],
    )
    outcome = await run_robot(manifest, heal_after_ms=200)
    assert outcome.result.state is RunState.SUCCEEDED
    healed = outcome.of(RunEventKind.STEP_HEALED, "digitar")
    assert healed[0].data["localizador"] == 1
    assert healed[0].data["contagens"] == [2, 1]


async def test_write_step_refuses_fallback_locator(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    manifest = _manifest(
        portal_v1,
        [
            {
                "kind": "navigate",
                "id": "abrir",
                "title": "Abrir",
                "url": f"{portal_v1.base_url}/login",
            },
            {
                "kind": "fill",
                "id": "gravar",
                "title": "Digitar num campo que grava",
                "effect": "write",
                "timeout_ms": 1_000,
                "target": _target(
                    "campo Login",
                    {"by": "label", "value": "Login"},
                    {"by": "css", "value": "input[name=usuario]"},
                ),
                "value": {"source": "text", "template": "fulano"},
            },
        ],
    )
    outcome = await run_robot(manifest)
    assert outcome.result.state is RunState.FAILED
    assert outcome.result.error_code == ErrorCode.LOCALIZADOR_ALTERNATIVO_EM_GRAVACAO
    assert not outcome.of(RunEventKind.STEP_HEALED)
    failed = outcome.of(RunEventKind.STEP_FAILED, "gravar")[0]
    assert failed.data["localizador"] == 1


# -- domínios e credenciais --------------------------------------------------------


async def test_navigate_outside_allowed_domains_is_refused(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    manifest = _manifest(
        portal_v1,
        [
            {
                "kind": "navigate",
                "id": "fugir",
                "title": "Abrir outro site",
                "url": f"http://outro.rapa.test:{portal_v1.port}/login",
            }
        ],
    )
    outcome = await run_robot(manifest)
    assert outcome.result.state is RunState.FAILED
    assert outcome.result.error_code == ErrorCode.DOMINIO_NAO_PERMITIDO
    assert outcome.result.failed_step_id == "fugir"
    assert "outro.rapa.test" in (outcome.result.error_message or "")


async def test_click_to_other_domain_is_blocked(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    page_url = f"{portal_v1.base_url}/saida"
    reached: list[str] = []

    async def prepare(context: BrowserContext) -> None:
        async def fake_page(route: Route) -> None:
            if route.request.url.startswith("http://fora.rapa.test"):
                reached.append(route.request.url)
            await route.fulfill(
                content_type="text/html",
                body='<a href="http://fora.rapa.test/coleta?x=1">Sair do portal</a>',
            )

        await context.route("**/*", fake_page)

    manifest = _manifest(
        portal_v1,
        [
            {"kind": "navigate", "id": "abrir", "title": "Abrir", "url": page_url},
            {
                "kind": "click",
                "id": "sair",
                "title": "Clicar no link",
                "target": _target(
                    "link", {"by": "role", "value": "link", "name": "Sair do portal"}
                ),
            },
        ],
    )
    outcome = await run_robot(manifest, prepare_context=prepare)
    assert outcome.result.state is RunState.FAILED
    assert outcome.result.error_code == ErrorCode.DOMINIO_NAO_PERMITIDO
    assert outcome.result.failed_step_id == "sair"
    assert reached == []  # a requisição nunca saiu do navegador


async def test_secret_is_not_typed_on_wrong_origin(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    other_origin = f"http://{portal_v1.host}:{portal_v1.port + 1}"
    manifest = _manifest(
        portal_v1,
        login_steps(portal_v1.base_url)[:2],
        credentials=[{"name": "portal", "kind": "password", "origin": other_origin}],
    )
    typed: list[str] = []

    async def inspect(context: BrowserContext) -> None:
        typed.append(await context.pages[0].get_by_label("Usuário").input_value())

    outcome = await run_robot(manifest, after_run=inspect)
    assert outcome.result.state is RunState.FAILED
    assert outcome.result.error_code == ErrorCode.CREDENCIAL_ORIGEM_INVALIDA
    assert outcome.result.failed_step_id == "digitar_usuario"
    assert typed == [""]
    assert other_origin in (outcome.result.error_message or "")
    assert_no_secrets(outcome.dump())


async def test_missing_secret_fails_without_typing(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    manifest = _manifest(portal_v1, login_steps(portal_v1.base_url)[:3])
    outcome = await run_robot(manifest, secrets={"portal": {"username": DEFAULT_USER}})
    assert outcome.result.state is RunState.FAILED
    assert outcome.result.error_code == ErrorCode.SEGREDO_INDISPONIVEL
    assert outcome.result.failed_step_id == "digitar_senha"


async def test_outputs_and_events_mask_credential_values(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    manifest = _manifest(
        portal_v1,
        [
            *login_steps(portal_v1.base_url),
            {
                "kind": "extract_text",
                "id": "ler_usuario",
                "title": "Ler o usuário logado",
                "target": _target("usuário logado", {"by": "test_id", "value": "usuario-logado"}),
                "into": "usuario",
            },
            {
                "kind": "assert",
                "id": "conferir",
                "title": "Conferir o usuário",
                "timeout_ms": 1_000,
                "condition": {"op": "equals", "left": "{{ vars.usuario }}", "right": "outro"},
                "message": f"O usuário {DEFAULT_USER} não confere",
            },
        ],
        outputs=[
            {"name": "usuario", "title": "Usuário", "type": "string", "source": "vars.usuario"}
        ],
    )
    outcome = await run_robot(manifest)
    assert outcome.result.error_code == ErrorCode.VERIFICACAO_FALHOU
    assert outcome.result.error_message == f"O usuário {MASK} não confere"
    outcome2 = await run_robot(edit(manifest, lambda data: data["steps"].pop()))
    assert outcome2.result.state is RunState.SUCCEEDED
    assert outcome2.result.outputs == {"usuario": MASK}
    assert_no_secrets(outcome.dump() + outcome2.dump())


# -- passo irreversível ---------------------------------------------------------------


async def test_failure_after_irreversible_step_is_indeterminate(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    manifest = set_step(
        emitir_protocolo(portal_v1.base_url, portal_v1.host),
        "ler_protocolo",
        timeout_ms=1_000,
        target=_target("número do protocolo", {"by": "test_id", "value": "protocolo-sumiu"}),
    )
    outcome = await run_robot(manifest, PROTOCOLO_INPUTS)
    result = outcome.result
    assert result.state is RunState.INDETERMINATE
    assert result.error_code == ErrorCode.ELEMENTO_NAO_ENCONTRADO
    assert result.failed_step_id == "ler_protocolo"
    assert result.irreversible_started
    assert "irreversível" in (result.error_message or "")
    assert "emitir" in (result.error_message or "")
    assert len(portal_v1.state.protocolos) == 1
    final = outcome.events[-1]
    assert final.kind is RunEventKind.RUN_FAILED
    assert final.data["estado"] == "indeterminate"
    assert final.data["passo_irreversivel"] == "emitir"


async def test_failed_postcondition_after_irreversible_step_is_indeterminate(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    def change(data: dict[str, Any]) -> None:
        data["postconditions"] = [{"op": "text_contains", "text": "Texto que nunca aparece"}]

    manifest = edit(emitir_protocolo(portal_v1.base_url, portal_v1.host), change)
    outcome = await run_robot(manifest, PROTOCOLO_INPUTS, postcondition_timeout_ms=500)
    result = outcome.result
    assert result.state is RunState.INDETERMINATE
    assert result.error_code == ErrorCode.POS_CONDICAO_FALHOU
    assert result.failed_step_id is None
    assert len(portal_v1.state.protocolos) == 1
    # O protocolo lido antes da falha continua no resultado, para a pessoa conferir.
    assert result.outputs == {"protocolo": portal_v1.state.protocolos[0]["numero"]}


async def test_irreversible_step_not_found_is_failed_not_indeterminate(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    manifest = emitir_protocolo(portal_v1.base_url, portal_v1.host)

    def change(data: dict[str, Any]) -> None:
        step = find_step(data, "emitir")
        step["timeout_ms"] = 1_000
        step["target"]["locators"][0]["name"] = "Emitir agora"

    outcome = await run_robot(edit(manifest, change), PROTOCOLO_INPUTS)
    assert outcome.result.state is RunState.FAILED
    assert outcome.result.error_code == ErrorCode.ELEMENTO_NAO_ENCONTRADO
    assert not outcome.result.irreversible_started
    assert portal_v1.state.protocolos == []
    started = outcome.of(RunEventKind.STEP_STARTED, "emitir")
    assert len(started) == 1 and "irreversivel" not in started[0].data


# -- fluxo de controle, quadros e esperas -----------------------------------------------


async def test_control_flow_frames_and_waits(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    base = portal_v1.base_url
    total = _target("total", {"by": "test_id", "value": "total"})
    frame = ["#quadro-consulta"]
    steps: list[dict[str, Any]] = [
        *login_steps(base),
        {
            "kind": "navigate",
            "id": "abrir_relatorio",
            "title": "Abrir o relatório",
            "url": f"{base}/lento",
        },
        {"kind": "wait_for", "id": "esperar_total", "title": "Esperar o total", "target": total},
        {
            "kind": "extract_text",
            "id": "ler_total",
            "title": "Ler o total",
            "target": total,
            "into": "total",
        },
        {
            "kind": "assert",
            "id": "conferir_pronto",
            "title": "Conferir que ficou pronto",
            "condition": {"op": "text_contains", "text": "Relatório pronto"},
            "message": "O relatório não ficou pronto",
        },
        {
            "kind": "if",
            "id": "se_relatorio",
            "title": "Se estiver no relatório",
            "condition": {"op": "url_contains", "value": "/lento"},
            "then": [
                {
                    "kind": "extract_text",
                    "id": "ler_status",
                    "title": "Ler o status",
                    "target": _target("status", {"by": "css", "value": "#status"}),
                    "into": "status",
                }
            ],
            "otherwise": [
                {"kind": "navigate", "id": "voltar", "title": "Voltar", "url": f"{base}/inicio"}
            ],
        },
        {
            "kind": "if",
            "id": "se_status_estranho",
            "title": "Se o status for outro",
            "condition": {"op": "equals", "left": "{{ vars.status }}", "right": "Processando…"},
            "then": [
                {"kind": "navigate", "id": "nunca", "title": "Nunca", "url": f"{base}/inicio"}
            ],
            "otherwise": [
                {
                    "kind": "wait_for",
                    "id": "esperar_sumir",
                    "title": "Esperar o aviso sumir",
                    "target": _target("aviso", {"by": "css", "value": "#nao-existe"}),
                    "state": "hidden",
                }
            ],
        },
        {
            "kind": "navigate",
            "id": "abrir_quadro",
            "title": "Abrir o quadro",
            "url": f"{base}/quadro",
        },
        {
            "kind": "click",
            "id": "confirmar_consulta",
            "title": "Confirmar a consulta",
            "target": _target(
                "botão Confirmar consulta",
                {"by": "role", "value": "button", "name": "Confirmar consulta"},
                frames=frame,
            ),
        },
        {
            "kind": "extract_text",
            "id": "ler_quadro",
            "title": "Ler o resultado",
            "target": _target(
                "resultado", {"by": "test_id", "value": "resultado-quadro"}, frames=frame
            ),
            "into": "quadro",
        },
        {"kind": "press", "id": "tab", "title": "Apertar Tab", "key": "Tab"},
        {
            "kind": "navigate",
            "id": "abrir_notas",
            "title": "Abrir as notas",
            "url": f"{base}/notas",
        },
        {
            "kind": "for_each",
            "id": "cada_numero",
            "title": "Para cada número",
            "items": "inputs.numeros",
            "steps": [
                {
                    "kind": "extract_text",
                    "id": "ler_cliente",
                    "title": "Ler o cliente da nota",
                    "target": _target(
                        "cliente da nota",
                        {"by": "css", "value": "td:nth-child(2)"},
                        within={"by": "role", "value": "row"},
                        within_has_text="{{ item.numero }}",
                    ),
                    "into": "cliente",
                }
            ],
        },
    ]
    manifest = _manifest(
        portal_v1,
        steps,
        inputs=[
            {
                "name": "numeros",
                "title": "Números",
                "type": "list",
                "item_fields": [{"name": "numero", "title": "Número", "type": "string"}],
            }
        ],
        outputs=[
            {"name": name, "title": name, "type": "string", "source": f"vars.{name}"}
            for name in ("total", "status", "quadro", "cliente")
        ],
        postconditions=[{"op": "url_contains", "value": "/notas"}],
    )
    numeros = [{"numero": "001001"}, {"numero": "001003"}]
    outcome = await run_robot(manifest, {"numeros": numeros})
    result = outcome.result
    assert result.state is RunState.SUCCEEDED, result.error_message
    assert result.outputs == {
        "total": "Total: 25 notas",
        "status": "Relatório pronto",
        "quadro": "Consulta confirmada",
        "cliente": "Clínica Bem Estar",
    }
    branches = {
        e.step_id: e.data["ramo"]
        for e in outcome.of(RunEventKind.STEP_SUCCEEDED)
        if "ramo" in e.data
    }
    assert branches == {"se_relatorio": "entao", "se_status_estranho": "senao"}
    assert len(outcome.of(RunEventKind.STEP_SUCCEEDED, "ler_cliente")) == 2
    assert not outcome.of(RunEventKind.STEP_STARTED, "nunca")


async def test_for_each_over_limit_fails(run_robot: RobotRunner, portal_v1: RunningPortal) -> None:
    manifest = _manifest(
        portal_v1,
        [
            {
                "kind": "for_each",
                "id": "cada",
                "title": "Para cada item",
                "items": "inputs.itens",
                "max_items": 2,
                "steps": [
                    {
                        "kind": "navigate",
                        "id": "abrir",
                        "title": "Abrir",
                        "url": f"{portal_v1.base_url}/",
                    }
                ],
            }
        ],
        inputs=[
            {
                "name": "itens",
                "title": "Itens",
                "type": "list",
                "item_fields": [{"name": "x", "title": "X", "type": "string"}],
            }
        ],
    )
    outcome = await run_robot(manifest, {"itens": [{"x": "1"}, {"x": "2"}, {"x": "3"}]})
    assert outcome.result.error_code == ErrorCode.LIMITE_DE_ITENS_EXCEDIDO
    assert outcome.result.failed_step_id == "cada"
    assert not outcome.of(RunEventKind.STEP_STARTED, "abrir")


async def test_missing_required_input_fails_before_any_step(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    manifest = emitir_protocolo(portal_v1.base_url, portal_v1.host)
    outcome = await run_robot(manifest, {"nome": "Maria"})
    assert outcome.result.state is RunState.FAILED
    assert outcome.result.error_code == ErrorCode.ENTRADA_INVALIDA
    assert "cpf" in (outcome.result.error_message or "")
    assert outcome.kinds() == [RunEventKind.RUN_STARTED, RunEventKind.RUN_FAILED]


# -- cancelamento, prazo e telas ----------------------------------------------------------


class _CancelAfter(ListEventSink):
    def __init__(self, step_id: str, cancel: asyncio.Event) -> None:
        super().__init__()
        self.step_id = step_id
        self.cancel = cancel

    async def emit(self, event: RunEvent) -> None:
        await super().emit(event)
        if event.kind is RunEventKind.STEP_SUCCEEDED and event.step_id == self.step_id:
            self.cancel.set()


async def test_cancel_between_steps(run_robot: RobotRunner, portal_v1: RunningPortal) -> None:
    cancel = asyncio.Event()
    sink = _CancelAfter("entrar", cancel)
    manifest = emitir_protocolo(portal_v1.base_url, portal_v1.host)
    outcome = await run_robot(manifest, PROTOCOLO_INPUTS, events=sink, cancel=cancel)
    result = outcome.result
    assert result.state is RunState.CANCELLED
    assert result.error_code == ErrorCode.CANCELADA
    assert result.failed_step_id == "digitar_codigo"
    assert not [
        e
        for e in sink.events
        if e.kind is RunEventKind.STEP_STARTED and e.step_id == "digitar_codigo"
    ]
    assert sink.events[-1].kind is RunEventKind.RUN_FAILED


async def test_run_deadline_is_tempo_esgotado(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    manifest = _manifest(
        portal_v1,
        [
            {
                "kind": "navigate",
                "id": "abrir",
                "title": "Abrir",
                "url": f"{portal_v1.base_url}/login",
            },
            {
                "kind": "wait_for",
                "id": "esperar",
                "title": "Esperar algo que não vem",
                "timeout_ms": 60_000,
                "target": _target("nada", {"by": "test_id", "value": "nunca-aparece"}),
            },
        ],
    )
    deadline = datetime.now(UTC) + timedelta(seconds=2)
    outcome = await run_robot(manifest, deadline=deadline)
    assert outcome.result.state is RunState.FAILED
    assert outcome.result.error_code == ErrorCode.TEMPO_ESGOTADO
    assert outcome.result.failed_step_id == "esperar"

    late = await run_robot(manifest, deadline=datetime.now(UTC) - timedelta(seconds=1))
    assert late.result.error_code == ErrorCode.TEMPO_ESGOTADO
    assert late.result.steps_done == 0


async def test_screenshot_after_every_step_when_asked(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    manifest = _manifest(portal_v1, login_steps(portal_v1.base_url)[:2])
    outcome = await run_robot(manifest, screenshot_every_step=True)
    assert outcome.result.state is RunState.SUCCEEDED
    names = [a.name for a in outcome.result.artifacts]
    assert names == ["tela-001-abrir_login-depois.png", "tela-002-digitar_usuario-depois.png"]
    assert {p.name for p in outcome.artifacts_dir.iterdir()} == set(names)


async def test_context_and_interpreter_are_single_use(
    browser: Browser, portal_v1: RunningPortal, tmp_path: Path
) -> None:
    manifest = _manifest(portal_v1, login_steps(portal_v1.base_url)[:1])

    def interpreter() -> Interpreter:
        return Interpreter(
            manifest,
            {},
            secrets=DictSecretProvider(SECRETS),
            files=LocalFileProvider(None),
            artifacts=DirectoryArtifactSink(tmp_path),
            events=ListEventSink(),
            run_id=new_id(),
            attempt=1,
        )

    async with run_context(browser) as context:
        first = interpreter()
        assert (await first.run(context)).state is RunState.SUCCEEDED
        with pytest.raises(RuntimeError, match="contexto"):
            await interpreter().run(context)
    async with run_context(browser) as context:
        with pytest.raises(RuntimeError, match="uma única vez"):
            await first.run(context)


# -- varredura de segredos ------------------------------------------------------------------


async def test_no_secret_value_appears_in_events_results_or_artifact_names(
    run_robot: RobotRunner, portal_v1: RunningPortal, portal_v2: RunningPortal
) -> None:
    v2_manifest = set_step(
        emitir_protocolo(portal_v2.base_url, portal_v2.host), "entrar", timeout_ms=1_000
    )
    outcomes = [
        await run_robot(emitir_protocolo(portal_v1.base_url, portal_v1.host), PROTOCOLO_INPUTS),
        await run_robot(baixar_notas(portal_v1.base_url, portal_v1.host)),
        await run_robot(v2_manifest, PROTOCOLO_INPUTS),  # falha, com tela de falha
    ]
    assert [o.result.state for o in outcomes] == [
        RunState.SUCCEEDED,
        RunState.SUCCEEDED,
        RunState.FAILED,
    ]
    for outcome in outcomes:
        assert outcome.result.artifacts
        assert_no_secrets(outcome.dump())
        for ref in outcome.result.artifacts:
            assert_no_secrets(ref.name + ref.path)
        # Nos passos que digitam, os eventos só dizem a origem e o tamanho do valor.
        for event in outcome.of(RunEventKind.STEP_SUCCEEDED):
            if "origem" in event.data:
                assert set(event.data) == {"duracao_ms", "origem", "tamanho"}
                assert event.data["origem"] in ("texto", "segredo")

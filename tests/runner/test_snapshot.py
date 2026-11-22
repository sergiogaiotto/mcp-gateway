"""Retrato da página no passo que falhou: só leitura, sem valores de campos nem segredos."""

import json
import time
from urllib.parse import quote

import pytest
from playwright.async_api import Browser

from rapa.core.contracts import RunState
from rapa.core.robot import Manifest
from rapa.core.snapshot import REDACTED, SNAPSHOT_ERROR_CODES
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER
from rapa.fixtures.robots import baixar_notas, emitir_protocolo
from rapa.fixtures.server import RunningPortal
from rapa.repair import enumerate_candidates
from rapa.runner.errors import ErrorCode
from rapa.runner.snapshot import Masker, capture, fit, flatten_strings, page_path
from tests.repair.support import element, snapshot
from tests.runner.support import (
    PROTOCOLO_INPUTS,
    RobotRunner,
    assert_no_secrets,
    edit,
    find_step,
    set_step,
)

pytestmark = pytest.mark.browser


def test_snapshot_codes_are_the_runner_codes() -> None:
    assert {
        ErrorCode.ELEMENTO_NAO_ENCONTRADO,
        ErrorCode.ELEMENTO_AMBIGUO,
        ErrorCode.LOCALIZADOR_ALTERNATIVO_EM_GRAVACAO,
    } == {ErrorCode(code) for code in SNAPSHOT_ERROR_CODES}


def test_masker_and_helpers_drop_values_and_queries() -> None:
    masker = Masker(["ana.souza", "abc", "Senha-Forte-123"])
    assert masker.text("Olá, ANA.SOUZA e senha-forte-123") == f"Olá, {REDACTED} e {REDACTED}"
    # Curto demais para mascarar: texto comum demais.
    assert masker.text("abc") == "abc"
    assert page_path("https://u:p@portal.test:8443/a/b?token=1#x") == "https://portal.test:8443/a/b"
    assert page_path("about:blank") == "about:"
    assert flatten_strings({"a": "x", "b": [{"c": "y"}, 3], "d": 1.5, "e": True}) == [
        "x",
        "y",
        "3",
        "1.5",
    ]


def test_big_snapshots_lose_the_last_elements() -> None:
    page = snapshot(
        "entrar",
        "click",
        *(element(tag="a", role="link", name=f"Item {i}", text="x" * 250) for i in range(150)),
    )
    small = fit(page, 20_000)
    assert small.truncated and 0 < len(small.elements) < 150
    assert len(small.model_dump_json()) <= 20_000
    assert [e.name for e in small.elements] == [
        e.name for e in page.elements[: len(small.elements)]
    ]
    assert fit(page) is page


async def test_v1_robot_on_v2_leaves_a_snapshot_of_the_login_page(
    run_robot: RobotRunner, portal_v2: RunningPortal
) -> None:
    manifest = set_step(
        emitir_protocolo(portal_v2.base_url, portal_v2.host), "entrar", timeout_ms=1_500
    )
    outcome = await run_robot(manifest, PROTOCOLO_INPUTS)
    result = outcome.result
    assert result.state is RunState.FAILED
    assert result.error_code == ErrorCode.ELEMENTO_NAO_ENCONTRADO
    snapshot = result.failure_snapshot
    assert snapshot is not None
    assert snapshot.step_id == "entrar" and snapshot.step_kind == "click"
    assert snapshot.error_code == "elemento_nao_encontrado"
    assert snapshot.url == f"{portal_v2.base_url}/login"
    buttons = [e for e in snapshot.elements if e.role == "button"]
    assert [(b.name, b.counts.role_name) for b in buttons] == [("Acessar", 1)]
    links = {e.name for e in snapshot.elements if e.role == "link"}
    assert links == {"Início", "Sair"}
    # Os campos do formulário não são alvo de um clique.
    assert not [e for e in snapshot.elements if e.tag == "input"]
    # O retrato só vai com a falha; nada foi enviado ao portal.
    assert portal_v2.state.protocolos == []
    assert_no_secrets(result.model_dump_json())


async def test_snapshot_of_a_fill_has_fields_without_their_values(
    run_robot: RobotRunner, portal_v2: RunningPortal
) -> None:
    # O usuário e a senha já foram digitados; o passo seguinte procura um campo que não
    # existe na página e falha ali.
    def change(data: dict[str, object]) -> None:
        step = find_step(data, "entrar")  # type: ignore[arg-type]
        step.clear()
        step.update(
            kind="fill",
            id="entrar",
            title="Digitar o apelido",
            timeout_ms=1_500,
            target={"intent": "campo Apelido", "locators": [{"by": "label", "value": "Apelido"}]},
            value={"source": "text", "template": "{{ inputs.nome }}"},
        )

    manifest = edit(emitir_protocolo(portal_v2.base_url, portal_v2.host), change)
    outcome = await run_robot(manifest, PROTOCOLO_INPUTS)
    snapshot = outcome.result.failure_snapshot
    assert snapshot is not None and snapshot.step_kind == "fill"
    fields = {e.label: e for e in snapshot.elements}
    assert set(fields) == {"Login", "Senha"}
    assert fields["Senha"].is_password and fields["Senha"].sensitive
    assert not fields["Login"].sensitive
    assert fields["Login"].counts.label == 1
    dump = snapshot.model_dump_json()
    # Os valores digitados (usuário e senha do cofre) nunca entram no retrato.
    for value in (DEFAULT_USER, DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET):
        assert value not in dump
    assert "value" not in json.loads(dump)["elements"][0]


async def test_page_text_with_secrets_and_inputs_is_masked(
    run_robot: RobotRunner, portal_v2: RunningPortal
) -> None:
    # Depois do login, a página inicial mostra o usuário ("Olá, ana.souza"); o passo que
    # falha procura um texto pelo data-testid.
    data = emitir_protocolo(portal_v2.base_url, portal_v2.host).model_dump(mode="json")
    steps = data["steps"]
    position = next(i for i, step in enumerate(steps) if step["id"] == "aguardar_inicio")
    data["steps"] = [
        *steps[: position + 1],
        {
            "kind": "extract_text",
            "id": "ler_saudacao",
            "title": "Ler a saudação",
            "timeout_ms": 1_000,
            "target": {
                "intent": "nome da pessoa",
                "locators": [{"by": "test_id", "value": "usuario-atual"}],
            },
            "into": "saudacao",
        },
    ]
    data["outputs"] = []
    data["postconditions"] = []
    entrar = next(step for step in data["steps"] if step["id"] == "entrar")
    entrar["target"] = {"intent": "botão Acessar", "locators": [{"by": "text", "value": "Acessar"}]}
    outcome = await run_robot(Manifest.model_validate(data), PROTOCOLO_INPUTS)
    result = outcome.result
    assert result.failed_step_id == "ler_saudacao", result.error_message
    snapshot = result.failure_snapshot
    assert snapshot is not None
    greeting = next(e for e in snapshot.elements if e.test_id == "usuario-logado")
    assert greeting.text == REDACTED
    assert DEFAULT_USER not in snapshot.model_dump_json()


async def test_success_and_other_failures_have_no_snapshot(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    ok = await run_robot(baixar_notas(portal_v1.base_url, portal_v1.host))
    assert ok.result.state is RunState.SUCCEEDED
    assert ok.result.failure_snapshot is None
    # Uma falha que não é do alvo (uma navegação para fora dos domínios) não tira retrato.
    manifest = set_step(
        emitir_protocolo(portal_v1.base_url, portal_v1.host),
        "abrir_login",
        url="https://fora.rapa.test/login",
    )
    failed = await run_robot(manifest, PROTOCOLO_INPUTS)
    assert failed.result.state is RunState.FAILED
    assert failed.result.failure_snapshot is None


async def test_snapshot_reaches_into_iframes_and_candidates_keep_the_frame(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    data = emitir_protocolo(portal_v1.base_url, portal_v1.host).model_dump(mode="json")
    steps = data["steps"]
    position = next(i for i, step in enumerate(steps) if step["id"] == "aguardar_inicio")
    data["steps"] = [
        *steps[: position + 1],
        {
            "kind": "navigate",
            "id": "abrir_quadro",
            "title": "Abrir a consulta",
            "url": f"{portal_v1.base_url}/quadro",
        },
        {
            "kind": "click",
            "id": "confirmar",
            "title": "Confirmar a consulta",
            "timeout_ms": 1_000,
            "target": {
                "intent": "botão Confirmar",
                "frames": ["#quadro-consulta"],
                "locators": [{"by": "role", "value": "button", "name": "Confirmar"}],
            },
        },
    ]
    data["outputs"] = []
    data["postconditions"] = []
    manifest = Manifest.model_validate(data)
    outcome = await run_robot(manifest, PROTOCOLO_INPUTS)
    snapshot = outcome.result.failure_snapshot
    assert snapshot is not None and snapshot.step_id == "confirmar"
    assert len(snapshot.frames) == 2
    assert snapshot.frames[1].path[0].id == "quadro-consulta"
    button = next(e for e in snapshot.elements if e.name == "Confirmar consulta")
    assert button.frame == 1
    step = manifest.step_by_id("confirmar")
    assert step is not None
    best = enumerate_candidates(snapshot, step)[0]
    assert best.name == "Confirmar consulta"
    assert best.frames == ["#quadro-consulta"]


def test_frame_src_with_a_credential_or_a_whole_page_is_left_out() -> None:
    from rapa.runner.snapshot import _frame_src

    assert _frame_src("https://relatorio:Tok3n@reports.test/x?y=1") is None
    assert _frame_src("data:text/html,<input value=segredo>") is None
    assert _frame_src("javascript:alert(1)") is None
    assert _frame_src("/relatorios/mensal?mes=11#topo") == "/relatorios/mensal"
    assert _frame_src("https://reports.test/x?y=1") == "https://reports.test/x"


async def test_editable_elements_and_textareas_leave_no_value_in_the_snapshot(
    browser: Browser,
) -> None:
    # Um editor de texto (contenteditable) com o que foi digitado, um link digitado dentro
    # de outro e um <textarea> com o valor vindo do servidor, numa célula de tabela.
    context = await browser.new_context()
    try:
        page = await context.new_page()
        await page.set_content(
            "<div contenteditable=true role=textbox aria-label=Descricao>Texto antigo</div>"
            "<div contenteditable=true role=textbox aria-label=Corpo>"
            "<a href='/x'>Link digitado</a></div>"
            "<table><tr><td>Obs</td><td><textarea aria-label=Obs>Nota guardada</textarea></td>"
            "<td><div contenteditable=true role=textbox aria-label=Resumo>Resumo salvo guardado</div>"
            "</td><td><a href='/y'>Abrir</a></td></tr></table>"
        )
        await page.get_by_role("textbox", name="Descricao").fill("Linha um\nLinha dois")
        snapshots = {
            kind: await capture(
                page, step_id="passo", step_kind=kind, error_code="elemento_nao_encontrado"
            )
            for kind in ("fill", "click", "extract_text")
        }
    finally:
        await context.close()
    for kind, taken in snapshots.items():
        assert taken is not None, kind
        dump = taken.model_dump_json()
        for value in ("Linha", "Texto antigo", "Link digitado", "Nota guardada", "guardado"):
            assert value not in dump, (kind, value)
    fields = {e.name: e for e in snapshots["fill"].elements}
    assert {"Descricao", "Corpo", "Obs"} <= set(fields)
    assert fields["Descricao"].text is None
    # O que não é valor de campo continua no retrato.
    opened = next(e for e in snapshots["click"].elements if e.name == "Abrir")
    assert opened.row is not None and opened.row.cells[:2] == ["Obs", ""]


async def test_numeric_inputs_are_masked_in_texts_and_in_the_path(
    browser: Browser, portal_v1: RunningPortal
) -> None:
    inputs = {"matricula": 482913, "contrato": {"numero": 77120045}}
    context = await browser.new_context()
    try:
        page = await context.new_page()
        await page.goto(f"{portal_v1.base_url}/contratos/77120045")
        await page.set_content("<button>Matricula 482913</button>")
        taken = await capture(
            page,
            step_id="passo",
            step_kind="click",
            error_code="elemento_nao_encontrado",
            mask_values=flatten_strings(inputs),
        )
    finally:
        await context.close()
    assert taken is not None
    dump = taken.model_dump_json()
    assert "482913" not in dump and "77120045" not in dump
    assert [e.name for e in taken.elements] == [f"Matricula {REDACTED}"]
    assert taken.url.endswith(f"/contratos/{REDACTED}")


def test_masker_drops_values_in_the_forms_the_snapshot_keeps() -> None:
    masker = Masker(["Linha um\nLinha  dois ", "Maria Silva", "João Souza"])
    # O texto da página chega com os brancos juntados; o endereço, codificado.
    assert masker.text("Obs: Linha um Linha dois.") == f"Obs: {REDACTED}."
    assert masker.text("https://portal.test/clientes/Maria%20Silva/notas") == (
        f"https://portal.test/clientes/{REDACTED}/notas"
    )
    assert masker.text("/c/jo%c3%a3o%20souza") == f"/c/{REDACTED}"


async def test_values_at_the_limit_and_in_the_path_are_masked(
    browser: Browser, portal_v1: RunningPortal
) -> None:
    login = "ana.souza@empresa.com.br"
    values = [login, "Maria Silva", "Linha um\nLinha dois"]
    context = await browser.new_context()
    try:
        page = await context.new_page()
        await page.goto(f"{portal_v1.base_url}/clientes/{quote('Maria Silva', safe='')}/notas")
        await page.set_content(
            f"<a href='/x'>{'a' * 292} {login}</a><button>Linha um\nLinha dois</button>"
        )
        taken = await capture(
            page,
            step_id="passo",
            step_kind="click",
            error_code="elemento_nao_encontrado",
            mask_values=values,
        )
    finally:
        await context.close()
    assert taken is not None
    dump = taken.model_dump_json()
    # Nem o começo do usuário no limite do texto, nem o nome no caminho, nem o texto
    # de várias linhas.
    for leaked in ("ana.sou", "Maria", "Linha"):
        assert leaked not in dump, leaked
    assert taken.url.endswith(f"/clientes/{REDACTED}/notas")
    link = next(e for e in taken.elements if e.role == "link")
    assert link.name is not None and len(link.name) == 300


async def test_masking_many_inputs_stays_within_the_snapshot_deadline(browser: Browser) -> None:
    # Uma lista grande nas entradas (30 000 textos) e uma tabela de linhas longas: a
    # limpeza dos textos passaria muito do prazo, sem nenhum await para interrompê-la.
    values = [f"item-{i:05d}-{j}-valor" for i in range(5_000) for j in range(6)]
    cells = "".join(f"<td>{'item-0 do item-1 ' * 12}</td>" for _ in range(20))
    rows = "".join(f"<tr><td><a href='/n/{i}'>Abrir {i}</a></td>{cells}</tr>" for i in range(40))
    context = await browser.new_context()
    try:
        page = await context.new_page()
        await page.set_content(f"<table>{rows}</table>")
        started = time.monotonic()
        taken = await capture(
            page,
            step_id="passo",
            step_kind="click",
            error_code="elemento_nao_encontrado",
            mask_values=values,
            timeout_s=0.5,
        )
        elapsed = time.monotonic() - started
    finally:
        await context.close()
    # Sem tempo para limpar tudo, o retrato fica de fora (nunca vai sem a limpeza).
    assert elapsed < 3, elapsed
    assert taken is None

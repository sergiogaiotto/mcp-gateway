"""Retrato da página no passo que falhou: só leitura, sem valores de campos nem segredos."""

import json

import pytest

from rapa.core.contracts import RunState
from rapa.core.robot import Manifest
from rapa.core.snapshot import REDACTED, SNAPSHOT_ERROR_CODES
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER
from rapa.fixtures.robots import baixar_notas, emitir_protocolo
from rapa.fixtures.server import RunningPortal
from rapa.repair import enumerate_candidates
from rapa.runner.errors import ErrorCode
from rapa.runner.snapshot import Masker, fit, flatten_strings, page_path
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
    assert flatten_strings({"a": "x", "b": [{"c": "y"}, 3], "d": 1}) == ["x", "y"]


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

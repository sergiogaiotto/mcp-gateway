import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from rapa.llm.capabilities import (
    Capability,
    CapabilityMatrix,
    CheckStatus,
    ReasoningShape,
    ensure_capabilities,
    matrix_changes,
)
from rapa.llm.errors import CapabilityMissing
from rapa.llm.probe import LARGE_MAX_TOKENS, main, probe, render_summary
from tests.llm.fakehub import API_KEY, BASE_URL, FakeHub, error, make_matrix

YES, NO, ERROR, SKIPPED = CheckStatus.YES, CheckStatus.NO, CheckStatus.ERROR, CheckStatus.SKIPPED


async def run_probe(hub: FakeHub, model: str = "qwen-latest") -> CapabilityMatrix:
    async with hub.client(model=model, max_retries=1) as client:
        return await probe(client)


async def test_good_hub() -> None:
    hub = FakeHub()
    matrix = await run_probe(hub)
    assert matrix.alias == "qwen-latest"
    assert matrix.models_listed == ["openai/gpt-oss-20b", "qwen-latest"]
    assert matrix.alias_listed.status is YES
    assert matrix.fingerprint == "Qwen/Qwen3.8-27B"
    assert matrix.baseline.status is YES
    assert matrix.reasoning_shape is ReasoningShape.REASONING_CONTENT
    assert matrix.rejects_unknown_fields.status is NO
    assert matrix.thinking_off_template_kwargs.status is YES
    assert matrix.thinking_off_reasoning_effort.status is NO
    assert matrix.thinking_off_reasoning_effort.accepted is True
    assert matrix.json_schema_enforced.status is YES
    assert matrix.tools_structured.status is YES
    assert matrix.tool_choice_required.status is YES
    assert matrix.parallel_tool_calls.status is YES
    assert matrix.image_input.status is YES
    assert matrix.max_tokens_above_documented.status is YES
    assert matrix.max_tokens_ceiling == LARGE_MAX_TOKENS
    assert matrix.logprobs.status is YES
    assert matrix.latency_ms is not None
    assert matrix.tokens_per_second is not None and matrix.tokens_per_second > 0
    assert matrix.errors == []
    # Uma requisição por verificação: /models e 11 chamadas de chat.
    assert len(hub.requests) == 1 + 11
    # Depois de descobrir como desligar o raciocínio, a sonda usa isso nas demais verificações.
    json_check = next(b for b in hub.chat_requests if "response_format" in b)
    assert json_check["chat_template_kwargs"] == {"enable_thinking": False}
    # A linha de base só usa os campos documentados.
    assert set(hub.chat_requests[0]) == {"model", "messages", "max_tokens", "temperature"}
    assert matrix.missing(list(Capability)) == []


async def test_hub_that_ignores_thinking_controls() -> None:
    matrix = await run_probe(FakeHub(honours_template_kwargs=False, honours_reasoning_effort=False))
    assert matrix.thinking_off_template_kwargs.status is NO
    assert matrix.thinking_off_template_kwargs.accepted is True
    assert "raciocinou" in matrix.thinking_off_template_kwargs.detail
    assert matrix.thinking_off_reasoning_effort.status is NO
    assert not matrix.has(Capability.THINKING_OFF)


async def test_hub_where_only_reasoning_effort_works() -> None:
    hub = FakeHub(honours_template_kwargs=False, honours_reasoning_effort=True)
    matrix = await run_probe(hub)
    assert matrix.thinking_off_template_kwargs.status is NO
    assert matrix.thinking_off_reasoning_effort.status is YES
    assert matrix.has(Capability.THINKING_OFF)
    json_check = next(b for b in hub.chat_requests if "response_format" in b)
    assert json_check["reasoning_effort"] == "low"
    assert "chat_template_kwargs" not in json_check


async def test_gateway_that_rejects_unknown_fields() -> None:
    hub = FakeHub(
        rejects_unknown_fields=True,
        known_extras=frozenset({"reasoning_effort", "response_format", "tools", "tool_choice"}),
        honours_reasoning_effort=True,
    )
    matrix = await run_probe(hub)
    assert matrix.rejects_unknown_fields.status is YES
    assert "rapa_sonda_campo_inexistente" in matrix.rejects_unknown_fields.detail
    assert matrix.thinking_off_template_kwargs.status is NO
    assert matrix.thinking_off_template_kwargs.accepted is False
    assert matrix.thinking_off_template_kwargs.http_status == 400
    assert matrix.thinking_off_reasoning_effort.status is YES
    # parallel_tool_calls e logprobs não são conhecidos por este gateway.
    assert matrix.parallel_tool_calls.status is NO
    assert matrix.parallel_tool_calls.accepted is False
    assert matrix.logprobs.status is NO
    assert any("enable_thinking" in error for error in matrix.errors)


@pytest.mark.parametrize(("mode", "accepted"), [("ignored", True), ("rejected", False)])
async def test_json_schema_not_enforced(mode: Any, accepted: bool) -> None:
    matrix = await run_probe(FakeHub(json_schema=mode))
    assert matrix.json_schema_enforced.status is NO
    assert matrix.json_schema_enforced.accepted is accepted
    assert not matrix.has(Capability.JSON_SCHEMA)


async def test_tool_calls_as_xml() -> None:
    matrix = await run_probe(FakeHub(tools="xml"))
    assert matrix.tools_structured.status is NO
    assert "XML" in matrix.tools_structured.detail
    assert matrix.tool_choice_required.status is NO
    assert matrix.parallel_tool_calls.status is SKIPPED


async def test_tools_rejected_or_partial() -> None:
    matrix = await run_probe(FakeHub(tools="rejected"))
    assert matrix.tools_structured.status is NO
    assert matrix.tools_structured.accepted is False
    assert matrix.parallel_tool_calls.status is SKIPPED

    matrix = await run_probe(FakeHub(parallel_tools=False, tool_choice_required=False))
    assert matrix.tools_structured.status is YES
    assert matrix.tool_choice_required.status is NO
    assert matrix.tool_choice_required.accepted is False
    assert matrix.parallel_tool_calls.status is NO
    assert "esperava 2" in matrix.parallel_tool_calls.detail


@pytest.mark.parametrize(("mode", "accepted"), [("rejected", False), ("ignored", True)])
async def test_image_not_supported(mode: Any, accepted: bool) -> None:
    matrix = await run_probe(FakeHub(images=mode))
    assert matrix.image_input.status is NO
    assert matrix.image_input.accepted is accepted


async def test_limits_and_logprobs() -> None:
    matrix = await run_probe(FakeHub(max_tokens_limit=8192, logprobs=False))
    assert matrix.max_tokens_above_documented.status is NO
    assert matrix.max_tokens_ceiling == 8192
    assert matrix.logprobs.status is NO
    assert matrix.logprobs.accepted is True


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("reasoning", ReasoningShape.REASONING),
        ("inline", ReasoningShape.INLINE_THINK),
        ("harmony", ReasoningShape.INLINE_HARMONY),
        ("hidden", ReasoningShape.UNKNOWN),
    ],
)
async def test_reasoning_shape(shape: Any, expected: ReasoningShape) -> None:
    matrix = await run_probe(FakeHub(reasoning_shape=shape))
    assert matrix.reasoning_shape is expected
    assert matrix.baseline.status is YES


async def test_model_without_reasoning() -> None:
    matrix = await run_probe(
        FakeHub(thinks_by_default=False, fingerprint="openai/gpt-oss-20b"), "openai/gpt-oss-20b"
    )
    assert matrix.reasoning_shape is ReasoningShape.NONE
    assert matrix.fingerprint == "openai/gpt-oss-20b"
    assert matrix.thinking_off_template_kwargs.status is YES


async def test_alias_not_listed_and_errors_tolerated() -> None:
    hub = FakeHub(model_ids=["outro"], script=[])
    matrix = await run_probe(hub, "qwen-latest")
    assert matrix.alias_listed.status is NO
    assert matrix.baseline.status is YES


async def test_wrong_key_stops_early_without_leaking() -> None:
    hub = FakeHub(api_key="sk-a-chave-certa-do-hub")
    matrix = await run_probe(hub)
    assert matrix.alias_listed.status is ERROR
    assert matrix.alias_listed.http_status == 401
    assert matrix.baseline.status is SKIPPED
    assert matrix.json_schema_enforced.status is SKIPPED
    assert len(hub.requests) == 1
    assert API_KEY not in matrix.model_dump_json()


async def test_server_errors_are_recorded() -> None:
    hub = FakeHub(script=[error(503, "fora do ar")] * 4)
    matrix = await run_probe(hub)
    assert matrix.alias_listed.status is ERROR
    assert matrix.baseline.status is ERROR
    assert matrix.baseline.http_status == 503
    # A sonda continua depois dos erros.
    assert matrix.rejects_unknown_fields.status is NO
    assert len(matrix.errors) == 2


# ---------------------------------------------------------------------------
# Matriz, portão e comparação
# ---------------------------------------------------------------------------


def test_matrix_round_trip(tmp_path: Path) -> None:
    matrix = make_matrix()
    path = tmp_path / "matriz.json"
    matrix.save(path)
    assert CapabilityMatrix.load(path) == matrix
    assert json.loads(path.read_text())["alias"] == "qwen-latest"


def test_ensure_capabilities() -> None:
    matrix = make_matrix(
        json_schema_enforced=make_matrix().logprobs.model_copy(update={"status": NO}),
    )
    ensure_capabilities(matrix, [Capability.BASELINE, "tools"])
    with pytest.raises(CapabilityMissing) as info:
        ensure_capabilities(
            matrix, [Capability.JSON_SCHEMA, Capability.LARGE_MAX_TOKENS, Capability.BASELINE]
        )
    message = info.value.message
    assert "saída JSON com esquema" in message
    assert "max_tokens acima de 8192" in message
    assert "resposta básica" not in message
    assert info.value.missing == ["json_schema", "large_max_tokens"]
    with pytest.raises(CapabilityMissing, match=r"não de 'openai/gpt-oss-20b'"):
        ensure_capabilities(matrix, [Capability.BASELINE], model="openai/gpt-oss-20b")
    with pytest.raises(CapabilityMissing, match="rode a sonda"):
        ensure_capabilities(None, [Capability.BASELINE])
    with pytest.raises(ValueError):
        ensure_capabilities(matrix, ["voar"])


def test_matrix_changes() -> None:
    before = make_matrix()
    after = make_matrix(
        fingerprint="Qwen/Qwen3.9-27B",
        image_input=make_matrix().logprobs.model_copy(update={"status": NO}),
    )
    changes = matrix_changes(before, after)
    assert any("modelo mudou" in change for change in changes)
    assert any("deixou de funcionar: entrada de imagem" in change for change in changes)
    assert matrix_changes(before, make_matrix()) == []


def test_render_summary() -> None:
    summary = render_summary(make_matrix())
    assert "Modelo: qwen-latest" in summary
    assert "- saída JSON com esquema (response_format json_schema): sim" in summary
    assert "via reasoning_effort: não" in summary


# ---------------------------------------------------------------------------
# Linha de comando
# ---------------------------------------------------------------------------


@pytest.fixture
def hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAPA_LLM_BASE_URL", BASE_URL)
    monkeypatch.setenv("RAPA_LLM_API_KEY", API_KEY)
    monkeypatch.delenv("RAPA_LLM_MODEL", raising=False)


@pytest.mark.usefixtures("hub_env")
def test_cli_writes_matrix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "matrix.json"
    code = main(["--model", "qwen-latest", "--out", str(out)], transport=FakeHub().transport())
    assert code == 0
    matrix = CapabilityMatrix.load(out)
    assert matrix.alias == "qwen-latest"
    assert matrix.baseline.status is YES
    captured = capsys.readouterr()
    assert "Matriz gravada" in captured.out
    assert API_KEY not in captured.out + captured.err


@pytest.mark.usefixtures("hub_env")
def test_cli_require_and_compare(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "matrix.json"
    code = main(
        [
            "--model",
            "qwen-latest",
            "--out",
            str(out),
            "--require",
            "json_schema",
            "--require",
            "image_input",
        ],
        transport=FakeHub(images="rejected").transport(),
    )
    assert code == 1
    assert "entrada de imagem" in capsys.readouterr().err

    previous = tmp_path / "anterior.json"
    make_matrix(fingerprint="Qwen/Qwen3.7-27B").save(previous)
    code = main(
        ["--model", "qwen-latest", "--out", str(out), "--compare", str(previous)],
        transport=FakeHub().transport(),
    )
    assert code == 1
    assert "modelo mudou" in capsys.readouterr().err


def test_cli_without_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("RAPA_LLM_BASE_URL", "RAPA_LLM_API_KEY", "RAPA_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert main(["--out", str(tmp_path / "m.json")]) == 2
    assert "RAPA_LLM_BASE_URL" in capsys.readouterr().err
    assert not (tmp_path / "m.json").exists()


def test_module_entry_point(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RAPA_LLM_")}
    completed = subprocess.run(  # noqa: S603 - comando fixo
        [sys.executable, "-m", "rapa.llm.probe", "--out", str(tmp_path / "m.json")],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 2
    assert "defina o endereço do hub" in completed.stderr

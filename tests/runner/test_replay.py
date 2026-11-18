"""Reexecuções seguidas: o mesmo robô, no mesmo portal, produz os mesmos passos.

A saída da Fase 0 pede que cada robô de teste rode 100 vezes seguidas sem LLM;
o teste longo fica atrás do marcador ``slow`` e de ``RAPA_RUN_SLOW=1``.
"""

import json
import os

import pytest

from rapa.core.contracts import RunState
from rapa.core.events import RunEvent, RunEventKind
from rapa.core.robot import Manifest
from rapa.fixtures.robots import baixar_notas, emitir_protocolo
from rapa.fixtures.server import RunningPortal
from tests.runner.support import PROTOCOLO_INPUTS, RobotRunner

pytestmark = pytest.mark.browser

run_slow = pytest.mark.skipif(
    os.environ.get("RAPA_RUN_SLOW") != "1", reason="teste longo: defina RAPA_RUN_SLOW=1"
)


def step_outcomes(events: list[RunEvent]) -> list[tuple[str, str | None, str]]:
    """O que cada passo fez, sem tempos (e sem o conteúdo das telas, que muda)."""
    out = []
    for event in events:
        data = {k: v for k, v in event.data.items() if k != "duracao_ms"}
        if event.kind is RunEventKind.ARTIFACT and data.get("tipo") == "screenshot":
            data = {"tipo": "screenshot", "nome": data.get("nome")}
        out.append((event.kind.value, event.step_id, json.dumps(data, sort_keys=True)))
    return out


async def _replay(
    run_robot: RobotRunner, manifest: Manifest, inputs: dict[str, str], times: int
) -> None:
    first: list[tuple[str, str | None, str]] | None = None
    for attempt in range(1, times + 1):
        outcome = await run_robot(manifest, inputs)
        assert outcome.result.state is RunState.SUCCEEDED, (attempt, outcome.result.error_message)
        assert not outcome.of(RunEventKind.STEP_HEALED), attempt
        outcomes = step_outcomes(outcome.events)
        if first is None:
            first = outcomes
        else:
            assert outcomes == first, f"a execução {attempt} divergiu da primeira"


async def test_baixar_notas_five_times_in_a_row(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    await _replay(run_robot, baixar_notas(portal_v1.base_url, portal_v1.host), {}, 5)
    assert len(portal_v1.state.downloads) == 5 * 10


@pytest.mark.slow
@run_slow
async def test_baixar_notas_100_times_in_a_row(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    await _replay(run_robot, baixar_notas(portal_v1.base_url, portal_v1.host), {}, 100)
    assert len(portal_v1.state.downloads) == 100 * 10


@pytest.mark.slow
@run_slow
async def test_emitir_protocolo_100_times_in_a_row(
    run_robot: RobotRunner, portal_v1: RunningPortal
) -> None:
    manifest = emitir_protocolo(portal_v1.base_url, portal_v1.host)
    await _replay(run_robot, manifest, PROTOCOLO_INPUTS, 100)
    assert len(portal_v1.state.protocolos) == 100

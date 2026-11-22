"""O robô gravado no portal v1 quebra no v2; o conserto acha o elemento certo, sem agir.

Cada conserto sai só do retrato que o executor tirou na falha: nada roda de novo para
montar os candidatos. O robô consertado (duas vezes: o botão de login e o botão de
emitir, que é irreversível) roda até o fim no v2 e emite o protocolo.
"""

import pytest

from rapa.core import policy
from rapa.core.contracts import RunState
from rapa.core.robot import ClickStep, Effect, Manifest
from rapa.fixtures.robots import emitir_protocolo
from rapa.fixtures.server import RunningPortal
from rapa.repair import apply_candidate, enumerate_candidates
from tests.runner.support import PROTOCOLO_INPUTS, RobotRunner, assert_no_secrets, set_step

pytestmark = pytest.mark.browser


def _fast(manifest: Manifest, *step_ids: str) -> Manifest:
    for step_id in step_ids:
        manifest = set_step(manifest, step_id, timeout_ms=1_500)
    return manifest


async def test_v1_robot_is_repaired_twice_and_runs_to_the_end_on_v2(
    run_robot: RobotRunner, portal_v2: RunningPortal
) -> None:
    manifest = _fast(emitir_protocolo(portal_v2.base_url, portal_v2.host), "entrar", "emitir")

    # 1. O botão "Entrar" virou "Acessar".
    first = await run_robot(manifest, PROTOCOLO_INPUTS)
    assert first.result.failed_step_id == "entrar"
    snapshot = first.result.failure_snapshot
    assert snapshot is not None
    step = manifest.step_by_id("entrar")
    assert step is not None
    candidates = enumerate_candidates(snapshot, step)
    assert [c.candidate_id for c in candidates] == [f"c{i}" for i in range(1, len(candidates) + 1)]
    best = candidates[0]
    assert (best.role, best.name) == ("button", "Acessar")
    assert best.target.locators[0].model_dump(exclude_none=True) == {
        "by": "role",
        "value": "button",
        "name": "Acessar",
        "exact": True,
    }
    assert all(c.score <= best.score for c in candidates)
    assert {c.role for c in candidates} <= {"button", "link"}
    repaired = apply_candidate(manifest, "entrar", best)
    assert policy.check(repaired) == []
    # Só o alvo daquele passo mudou.
    before = manifest.model_dump(mode="json")["steps"]
    after = repaired.model_dump(mode="json")["steps"]
    changed = [old["id"] for old, new in zip(before, after, strict=True) if old != new]
    assert changed == ["entrar"]
    entrar = repaired.step_by_id("entrar")
    assert isinstance(entrar, ClickStep) and entrar.effect is Effect.READ
    assert portal_v2.state.protocolos == []

    # 2. O botão irreversível "Emitir" virou "Emitir documento": o passo falha antes de agir.
    second = await run_robot(repaired, PROTOCOLO_INPUTS)
    assert second.result.state is RunState.FAILED
    assert second.result.failed_step_id == "emitir"
    assert not second.result.irreversible_started
    assert portal_v2.state.protocolos == []
    snapshot = second.result.failure_snapshot
    assert snapshot is not None
    emitir = repaired.step_by_id("emitir")
    assert emitir is not None
    candidates = enumerate_candidates(snapshot, emitir)
    best = candidates[0]
    assert (best.role, best.name) == ("button", "Emitir documento")
    # Os campos de texto e as listas do formulário nunca são candidatos de um clique.
    assert all(c.role not in ("textbox", "combobox") for c in candidates)
    assert "checkbox" in {c.role for c in candidates}
    fixed = apply_candidate(repaired, "emitir", best)
    fixed_step = fixed.step_by_id("emitir")
    assert fixed_step is not None and fixed_step.effect is Effect.IRREVERSIBLE

    # 3. O robô consertado roda até o fim no v2.
    third = await run_robot(fixed, PROTOCOLO_INPUTS)
    assert third.result.state is RunState.SUCCEEDED, third.result.error_message
    assert third.result.failure_snapshot is None
    assert len(portal_v2.state.protocolos) == 1
    for outcome in (first, second, third):
        assert_no_secrets(outcome.result.model_dump_json())

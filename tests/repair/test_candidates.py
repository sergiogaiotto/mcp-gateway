"""Candidatos do conserto: só do retrato, pelo tipo do passo, com localizadores do compilador."""

import pytest

from rapa.core import policy
from rapa.core.robot import DownloadStep, ForEachStep, LocatorBy
from rapa.core.snapshot import REDACTED
from rapa.repair import NotRepairable, apply_candidate, enumerate_candidates, step_info
from tests.repair.support import BASE, element, login_page, notas, protocolo, snapshot


def _step(step_id: str):  # type: ignore[no-untyped-def]
    step = protocolo().step_by_id(step_id)
    assert step is not None
    return step


def test_click_prefers_the_button_with_a_synonym_and_ignores_fields() -> None:
    candidates = enumerate_candidates(login_page(), _step("entrar"))
    assert [(c.candidate_id, c.role, c.name) for c in candidates] == [
        ("c1", "button", "Acessar"),
        ("c2", "link", "Início"),
        ("c3", "link", "Sair"),
    ]
    best = candidates[0]
    assert best.description == "botão “Acessar”"
    assert best.score > candidates[1].score
    assert "mesmo papel de antes (button)" in best.reasons
    assert [loc.by for loc in best.target.locators] == [LocatorBy.ROLE, LocatorBy.TEXT]
    # Determinístico: o mesmo retrato dá a mesma lista.
    assert enumerate_candidates(login_page(), _step("entrar")) == candidates


def test_vault_password_only_goes_to_the_login_password_field() -> None:
    page = login_page("digitar_senha", "fill")
    candidates = enumerate_candidates(page, _step("digitar_senha"))
    assert [(c.label, c.sensitive) for c in candidates] == [("Senha", True)]
    user = enumerate_candidates(login_page("digitar_usuario", "fill"), _step("digitar_usuario"))
    assert [c.label for c in user] == ["Login"]
    assert user[0].target.locators[0].by is LocatorBy.LABEL
    # Um texto digitado nunca vai para o campo da senha de um login.
    nome = enumerate_candidates(login_page("digitar_nome", "fill"), _step("digitar_nome"))
    assert "Senha" not in {c.label for c in nome}


def test_redacted_text_never_becomes_a_locator_and_disabled_buttons_are_out() -> None:
    page = snapshot(
        "entrar",
        "click",
        element(
            tag="button",
            role="button",
            name=f"Entrar como {REDACTED}",
            text=f"Entrar como {REDACTED}",
            test_id="entrar",
            counts={"role_name": 1, "text": 1, "test_id": 1},
        ),
        element(tag="button", role="button", name="Entrar", enabled=False, counts={"role_name": 1}),
        # Só seria achado pelo caminho CSS: fica de fora.
        element(tag="button", role="button", counts={}),
    )
    candidates = enumerate_candidates(page, _step("entrar"))
    assert len(candidates) == 1
    assert [(loc.by, loc.value) for loc in candidates[0].target.locators] == [
        (LocatorBy.TEST_ID, "entrar")
    ]


def test_ambiguous_names_need_another_unique_locator() -> None:
    page = snapshot(
        "entrar",
        "click",
        element(tag="button", role="button", name="Acessar", counts={"role_name": 2}),
        element(
            tag="button",
            role="button",
            name="Acessar",
            id="acessar",
            counts={"role_name": 2, "id": 1},
        ),
    )
    candidates = enumerate_candidates(page, _step("entrar"))
    assert [[(loc.by, loc.value) for loc in c.target.locators] for c in candidates] == [
        [(LocatorBy.CSS, "#acessar")]
    ]


def test_row_step_keeps_its_row_and_only_row_wide_locators() -> None:
    manifest = notas()
    loop = manifest.step_by_id("cada_nota")
    assert isinstance(loop, ForEachStep)
    step = loop.steps[0]
    assert isinstance(step, DownloadStep)

    def row(numero: str) -> dict[str, object]:
        return {
            "text": f"Cliente {numero}\t{numero}\tAutorizada\t1,00\tDownload",
            "cells": [f"Cliente {numero}", numero, "Autorizada", "1,00", "Download"],
            "key": numero,
            "role_name_count": 1,
            "text_count": 1,
        }

    page = snapshot(
        "baixar_nota",
        "download",
        *(
            element(
                tag="a",
                role="link",
                name="Download",
                text="Download",
                row=row(numero),
                counts={"role_name": 10, "text": 10},
            )
            for numero in ("0001", "0002")
        ),
        # O link que leva a chave da linha no nome não serve às outras linhas.
        element(
            tag="a",
            role="link",
            name="nota-0003.pdf",
            text="nota-0003.pdf",
            row=row("0003"),
            counts={"role_name": 1, "text": 1},
        ),
        element(tag="a", role="link", name="Próxima", text="Próxima", counts={"role_name": 1}),
    )
    candidates = enumerate_candidates(page, step)
    assert len(candidates) == 1
    target = candidates[0].target
    assert target.within == step.target.within
    assert target.within_has_text == "{{ item.numero }}"
    assert [(loc.by, loc.value, loc.name) for loc in target.locators] == [
        (LocatorBy.ROLE, "link", "Download"),
        (LocatorBy.TEXT, "Download", None),
    ]
    repaired = apply_candidate(manifest, "baixar_nota", candidates[0])
    assert policy.check(repaired) == []


def test_frames_become_frame_selectors() -> None:
    page = snapshot(
        "entrar",
        "click",
        element(frame=1, tag="button", role="button", name="Acessar", counts={"role_name": 1}),
        frames=[{}, {"path": [{"id": "quadro-login", "title": "Login"}], "url": BASE}],
    )
    (candidate,) = enumerate_candidates(page, _step("entrar"))
    assert candidate.frames == ["#quadro-login"]
    assert "em outro quadro (iframe)" in candidate.reasons


def test_steps_without_target_and_snapshots_of_other_steps_are_refused() -> None:
    with pytest.raises(NotRepairable, match="não tem um elemento-alvo"):
        enumerate_candidates(login_page("abrir_login", "navigate"), _step("abrir_login"))
    with pytest.raises(NotRepairable, match="outro passo"):
        enumerate_candidates(login_page("emitir"), _step("entrar"))
    with pytest.raises(NotRepairable, match="não existe"):
        candidate = enumerate_candidates(login_page(), _step("entrar"))[0]
        apply_candidate(protocolo(), "passo_que_nao_existe", candidate)


def test_apply_changes_only_that_target() -> None:
    manifest = protocolo()
    candidate = enumerate_candidates(login_page(), _step("entrar"))[0]
    repaired = apply_candidate(manifest, "entrar", candidate)
    before = manifest.model_dump(mode="json")
    after = repaired.model_dump(mode="json")
    entrar_before = next(s for s in before["steps"] if s["id"] == "entrar")
    entrar_after = next(s for s in after["steps"] if s["id"] == "entrar")
    assert {k: v for k, v in entrar_before.items() if k != "target"} == {
        k: v for k, v in entrar_after.items() if k != "target"
    }
    for data in (before, after):
        data["steps"] = [s for s in data["steps"] if s["id"] != "entrar"]
    assert before == after
    info = step_info(_step("entrar"))
    assert info.intent == "botão Entrar" and info.kind == "click"

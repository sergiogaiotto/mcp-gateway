from typing import Any

import pytest

from rapa.core import policy
from rapa.core.robot import Manifest
from rapa.fixtures.robots import baixar_notas, emitir_protocolo

BASE = "http://portal-v1.rapa.test:8080"
HOST = "portal-v1.rapa.test"


def codes(data: dict[str, Any]) -> list[str]:
    return [v.code for v in policy.check(Manifest.model_validate(data))]


def protocolo() -> dict[str, Any]:
    return emitir_protocolo(BASE, HOST).model_dump(mode="json")


def test_sample_robots_pass() -> None:
    assert policy.check(emitir_protocolo(BASE, HOST)) == []
    assert policy.check(baixar_notas(BASE, HOST)) == []


def test_first_write_step() -> None:
    assert policy.first_write_step(emitir_protocolo(BASE, HOST)) == "emitir"
    assert policy.first_write_step(baixar_notas(BASE, HOST)) is None


def test_navigation_outside_allowed_domains() -> None:
    data = protocolo()
    data["steps"][0]["url"] = "https://outro.exemplo.com/login"
    assert "dominio_nao_permitido" in codes(data)


def test_dynamic_host_is_rejected() -> None:
    data = protocolo()
    data["steps"][0]["url"] = "https://{{ inputs.nome }}.rapa.test/login"
    assert "dominio_dinamico" in codes(data)


def test_http_only_on_test_domain() -> None:
    data = protocolo()
    data["allowed_domains"].append("portal.exemplo.com.br")
    data["steps"][0]["url"] = "http://portal.exemplo.com.br/login"
    assert "endereco_sem_tls" in codes(data)


def test_unknown_input_and_var() -> None:
    data = protocolo()
    data["steps"][8]["value"]["template"] = "{{ inputs.nao_existe }}"
    data["steps"][9]["value"]["template"] = "{{ vars.protocolo }}"
    found = codes(data)
    assert "entrada_desconhecida" in found
    assert "variavel_desconhecida" in found


def test_unknown_credential_and_wrong_field() -> None:
    data = protocolo()
    data["steps"][1]["value"]["credential"] = "outra"
    data["steps"][4]["value"]["field"] = "password"
    found = codes(data)
    assert "credencial_desconhecida" in found
    assert "credencial_campo_invalido" in found


def test_credential_must_belong_to_allowed_domain() -> None:
    data = protocolo()
    data["credentials"][0]["origin"] = "https://banco.exemplo.com.br"
    assert "credencial_fora_dos_dominios" in codes(data)


def test_item_outside_loop() -> None:
    data = protocolo()
    data["steps"][8]["value"]["template"] = "{{ item.numero }}"
    assert "item_fora_de_repeticao" in codes(data)


def test_loop_over_table_without_columns() -> None:
    data = baixar_notas(BASE, HOST).model_dump(mode="json")
    data["steps"][8]["columns"] = None
    assert "tabela_sem_colunas" in codes(data)


def test_output_without_source() -> None:
    data = protocolo()
    data["outputs"][0]["source"] = "vars.nada"
    assert "saida_sem_origem" in codes(data)


def test_duplicate_step_ids() -> None:
    data = protocolo()
    data["steps"][1]["id"] = "abrir_login"
    assert "passo_repetido" in codes(data)


# -- valores dinâmicos em seletores e endereços ----------------------------------------


@pytest.mark.parametrize(
    ("by", "value"),
    [
        ("css", 'button[data-nota="{{ inputs.nome }}"]'),
        ("xpath", "//button[@data-nota='{{ inputs.nome }}']"),
    ],
)
def test_dynamic_css_or_xpath_locator_is_rejected(by: str, value: str) -> None:
    data = protocolo()
    data["steps"][8]["target"]["locators"].append({"by": by, "value": value})
    assert "seletor_dinamico" in codes(data)


def test_dynamic_css_container_is_rejected() -> None:
    data = baixar_notas(BASE, HOST).model_dump(mode="json")
    target = data["steps"][9]["steps"][0]["target"]
    target["within"] = {"by": "css", "value": "tr[data-numero='{{ item.numero }}']"}
    target["within_has_text"] = None
    assert "seletor_dinamico" in codes(data)


def test_literal_css_and_dynamic_role_name_are_accepted() -> None:
    data = protocolo()
    target = data["steps"][8]["target"]
    target["locators"].append({"by": "css", "value": "input[data-x='{{{{']"})
    target["locators"].append({"by": "role", "value": "textbox", "name": "{{ inputs.nome }}"})
    assert codes(data) == []


def test_dynamic_port_is_rejected() -> None:
    data = protocolo()
    data["steps"][0]["url"] = "http://portal-v1.rapa.test:{{ inputs.nome }}/login"
    assert "dominio_dinamico" in codes(data)
